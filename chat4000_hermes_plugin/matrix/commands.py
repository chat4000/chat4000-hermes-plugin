"""Control-room command handler (protocol E — session commands + plugin self-update).

Handles the decrypted `chat4000.command` events the session routes from the
control room, and answers with `chat4000.command_result` (encrypted, push:false)
in the same room.

  session.new      → create an encrypted session room, invite the user, reply room_id
  session.rename   → set m.room.name
  session.archive  → tag low-priority
  plugin.update_check → read-only version report
  plugin.update    → DEFERRED (owner model undefined, pushback X4) → ok:false

`plugin.update` is owner-gated; until the registrar gives us a way to establish
and prove an owner identity, we refuse it with a clear error rather than guess.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from .registrar_client import RegistrarError
from .rooms import DEFAULT_SESSION_ROOM_NAME

if TYPE_CHECKING:
    from .crypto_driver import CryptoDriver
    from .rooms import RoomManager
    from .session import MatrixSession

logger = logging.getLogger(__name__)
DEVICE_PAIR_TTL_SECONDS = 120
PAIR_STATUS_POLL_INTERVAL_S = 1.5


class DevicePairRegistrarClient(Protocol):
    async def register(
        self,
        code: str,
        *,
        kind: str = "user",
        plugin_id: str | None = None,
        user_id: str | None = None,
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]: ...

    async def status(self, code: str) -> dict[str, Any]: ...


@dataclass
class PendingPairing:
    pair_id: str
    code: str
    sender: str
    deadline: float
    task: asyncio.Task[None]


class CommandHandler:
    def __init__(
        self,
        session: MatrixSession,
        *,
        owner_user_id: str | None = None,
        version: str = "0.0.0",
        registrar: DevicePairRegistrarClient | None = None,
    ) -> None:
        self._s = session
        self._owner = owner_user_id
        self._version = version
        self._registrar = registrar
        self._pairings: dict[str, PendingPairing] = {}

    @property
    def _rooms(self) -> RoomManager:
        if self._s.rooms is None:
            raise RuntimeError("command handler used before the session built its rooms")
        return self._s.rooms

    @property
    def _crypto(self) -> CryptoDriver:
        if self._s.crypto is None:
            raise RuntimeError("command handler used before the session built its crypto driver")
        return self._s.crypto

    async def handle(
        self, room_id: str, command: str, content: dict[str, Any], *, sender: str = ""
    ) -> None:
        try:
            if command == "session.new":
                await self._session_new(content)
            elif command == "session.rename":
                await self._session_rename(content)
            elif command == "session.delete":
                await self._session_delete(content)
            elif command == "session.archive":
                await self._session_archive(content)
            elif command == "plugin.update_check":
                await self._update_check()
            elif command == "plugin.update":
                await self._update(content)
            elif command == "device.pair_start":
                await self._device_pair_start(sender)
            elif command == "device.pair_cancel":
                await self._device_pair_cancel(content)
            else:
                await self._reply(command, {"ok": False, "error": f"unknown command {command!r}"})
        except Exception as exc:  # noqa: BLE001
            # Command-dispatch boundary: report the unexpected failure once to the
            # sink, then surface a clean error_result to the control room.
            from ..error_log import dump_chat4000_trace

            logger.warning("command %s failed: %s", command, exc)
            dump_chat4000_trace("matrix.command", exc, {"command": command})
            await self._reply(command, {"ok": False, "error": str(exc)})

    # ─── handlers ─────────────────────────────────────────────────────────

    async def _session_new(self, content: dict[str, Any]) -> None:
        import time

        title = content.get("title") or DEFAULT_SESSION_ROOM_NAME
        agent_id = content.get("agent_id") or "main"
        t0 = time.monotonic()
        logger.debug("session.new: creating room title=%r agent_id=%s", title, agent_id)
        room_id = await self._rooms.create_session_room_and_invite(self._s.members, title, agent_id)
        logger.debug(
            "session.new: room=%s created + %d member(s) invited in %.0fms",
            room_id,
            len(self._s.members),
            (time.monotonic() - t0) * 1000,
        )
        await self._reply("session.new", {"ok": True, "room_id": room_id})
        logger.debug(
            "session.new: replied room_id=%s (total %.0fms)",
            room_id,
            (time.monotonic() - t0) * 1000,
        )

    async def _session_rename(self, content: dict[str, Any]) -> None:
        room_id = content.get("room_id")
        title = content.get("title")
        if not room_id or not title:
            await self._reply(
                "session.rename", {"ok": False, "error": "room_id and title required"}
            )
            return
        await self._rooms.rename_session(room_id, title)
        await self._reply("session.rename", {"ok": True, "room_id": room_id})

    async def _session_archive(self, content: dict[str, Any]) -> None:
        room_id = content.get("room_id")
        if not room_id:
            await self._reply("session.archive", {"ok": False, "error": "room_id required"})
            return
        await self._rooms.archive_session(room_id)
        await self._reply("session.archive", {"ok": True, "room_id": room_id})

    async def _session_delete(self, content: dict[str, Any]) -> None:
        room_id = content.get("room_id")
        if not room_id:
            await self._reply("session.delete", {"ok": False, "error": "room_id required"})
            return
        await self._rooms.delete_session(room_id)
        await self._reply("session.delete", {"ok": True, "room_id": room_id})

    async def _device_pair_start(self, sender: str) -> None:
        pair_id = _gen_pair_id()
        if not sender:
            await self._fail_pair_start(pair_id, "event sender missing")
            return
        plugin_id = getattr(self._s, "plugin_id", None)
        if not plugin_id:
            await self._fail_pair_start(pair_id, "plugin_id missing")
            return

        code = _gen_device_pair_code()
        registrar = self._registrar_client()
        try:
            await registrar.register(
                code,
                kind="user",
                plugin_id=plugin_id,
                user_id=sender,
                ttl_seconds=DEVICE_PAIR_TTL_SECONDS,
            )
        except RegistrarError as exc:
            await self._fail_pair_start(pair_id, str(exc))
            return

        deadline = time.monotonic() + DEVICE_PAIR_TTL_SECONDS
        await self._reply("device.pair_start", {"pair_id": pair_id, "code": code})
        task = asyncio.create_task(self._poll_pairing(pair_id, code, deadline))
        self._pairings[pair_id] = PendingPairing(
            pair_id=pair_id,
            code=code,
            sender=sender,
            deadline=deadline,
            task=task,
        )

    async def _fail_pair_start(self, pair_id: str, error: str) -> None:
        fields = {"pair_id": pair_id, "error": _field_error(error)}
        await self._reply("device.pair_start", fields)
        await self._pair_status(pair_id, "error", error=error)

    async def _device_pair_cancel(self, content: dict[str, Any]) -> None:
        pair_id = content.get("pair_id")
        if not isinstance(pair_id, str) or not pair_id or len(pair_id) > 64:
            await self._reply(
                "device.pair_cancel",
                {"pair_id": str(pair_id or "")[:64], "error": "valid pair_id required"},
            )
            return
        attempt = self._pairings.pop(pair_id, None)
        if attempt is None:
            await self._reply(
                "device.pair_cancel", {"pair_id": pair_id, "error": "unknown pair_id"}
            )
            return
        attempt.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await attempt.task
        await self._reply("device.pair_cancel", {"pair_id": pair_id})
        await self._pair_status(pair_id, "cancelled")

    async def _poll_pairing(self, pair_id: str, code: str, deadline: float) -> None:
        registrar = self._registrar_client()
        try:
            while time.monotonic() < deadline:
                status = await registrar.status(code)
                state = status.get("status")
                if state == "completed":
                    await self._pair_status(pair_id, "completed")
                    return
                if state == "expired":
                    await self._pair_status(pair_id, "expired")
                    return
                if state not in ("pending", None):
                    await self._pair_status(pair_id, "error", error=f"unknown status {state!r}")
                    return
                sleep_for = min(PAIR_STATUS_POLL_INTERVAL_S, max(0.0, deadline - time.monotonic()))
                if sleep_for <= 0:
                    break
                await asyncio.sleep(sleep_for)
            await self._pair_status(pair_id, "expired")
        except asyncio.CancelledError:
            raise
        except RegistrarError as exc:
            await self._pair_status(pair_id, "error", error=str(exc))
        except Exception as exc:  # noqa: BLE001
            from ..error_log import dump_chat4000_trace

            dump_chat4000_trace("matrix.device_pair_status", exc, {"pair_id": pair_id})
            await self._pair_status(pair_id, "error", error=str(exc))
        finally:
            self._pairings.pop(pair_id, None)

    async def _pair_status(self, pair_id: str, state: str, *, error: str | None = None) -> None:
        control = self._rooms.control_room_id
        if control is None:
            logger.warning("no control room; cannot send pair status %s", state)
            return
        content: dict[str, Any] = {
            "msgtype": "chat4000.pair_status",
            "pair_id": pair_id,
            "state": state,
        }
        if error is not None:
            content["error"] = _field_error(error)
        await self._crypto.send_room_event(
            control, "m.room.message", content, self._s.recipients(control), push=False
        )

    def _registrar_client(self) -> DevicePairRegistrarClient:
        if self._registrar is not None:
            return self._registrar
        from ..cli import _registrar

        return _registrar()

    async def _update_check(self) -> None:
        # Read-only. We don't (yet) resolve a latest version or restart method —
        # report current + not-updatable until plugin.update is designed (X4).
        await self._reply(
            "plugin.update_check",
            {
                "ok": True,
                "current_version": self._version,
                "latest_version": self._version,
                "updatable": False,
                "restart_method": "unknown",
                "blockers": ["plugin.update not implemented (owner model undefined — X4)"],
            },
        )

    async def _update(self, content: dict[str, Any]) -> None:
        # Owner-gated remote code update over chat. Deferred until the registrar
        # defines owner establishment/proof (X4). Refuse cleanly.
        await self._reply(
            "plugin.update",
            {"ok": False, "error": "plugin.update is not supported yet (owner model undefined)"},
        )

    # ─── reply ────────────────────────────────────────────────────────────

    async def _reply(self, command: str, fields: dict[str, Any]) -> None:
        # Coarse, content-free: which command ran and whether it succeeded.
        try:
            from .. import analytics

            analytics.track("command_handled", {"command": command, "ok": fields.get("ok")})
        except Exception as exc:  # noqa: BLE001
            # Analytics is best-effort; report once and keep replying.
            from ..error_log import dump_chat4000_trace

            dump_chat4000_trace("matrix.command_analytics", exc)
        control = self._rooms.control_room_id
        if control is None:
            logger.warning("no control room; cannot reply to %s", command)
            return
        content = {"msgtype": "chat4000.command_result", "command": command, **fields}
        await self._crypto.send_room_event(
            control, "m.room.message", content, self._s.recipients(control), push=False
        )


def _gen_device_pair_code() -> str:
    """A 6-digit CSPRNG OTP (always exactly 6 digits)."""
    return f"{secrets.randbelow(900000) + 100000:06d}"


def _gen_pair_id() -> str:
    return f"p_{secrets.token_hex(6)}"


def _field_error(error: str) -> str:
    return str(error)[:255]
