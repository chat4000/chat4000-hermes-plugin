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

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .crypto_driver import CryptoDriver
    from .rooms import RoomManager
    from .session import MatrixSession

logger = logging.getLogger(__name__)


class CommandHandler:
    def __init__(
        self,
        session: MatrixSession,
        *,
        owner_user_id: str | None = None,
        version: str = "0.0.0",
    ) -> None:
        self._s = session
        self._owner = owner_user_id
        self._version = version

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

    async def handle(self, room_id: str, command: str, content: dict[str, Any]) -> None:
        try:
            if command == "session.new":
                await self._session_new(content)
            elif command == "session.rename":
                await self._session_rename(content)
            elif command == "session.archive":
                await self._session_archive(content)
            elif command == "plugin.update_check":
                await self._update_check()
            elif command == "plugin.update":
                await self._update(content)
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

        title = content.get("title") or "session"
        agent_id = content.get("agent_id") or "main"
        t0 = time.monotonic()
        logger.debug("session.new: creating room title=%r agent_id=%s", title, agent_id)
        room_id = await self._rooms.create_session_room(title, agent_id)
        t_created = time.monotonic()
        for uid in self._s.members:
            await self._rooms.invite_user(room_id, uid)
        t_invited = time.monotonic()
        logger.debug(
            "session.new: room=%s created in %.0fms, invited %d member(s) in %.0fms",
            room_id,
            (t_created - t0) * 1000,
            len(self._s.members),
            (t_invited - t_created) * 1000,
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
