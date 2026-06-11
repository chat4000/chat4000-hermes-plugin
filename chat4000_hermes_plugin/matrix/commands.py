"""Control-room command handler (protocol E — session commands + plugin self-update).

Handles the decrypted `chat4000.command` events the session routes from the
control room, and answers with `chat4000.command_result` (encrypted, push:false)
in the same room.

  session.new      → create an encrypted session room, invite the user, reply room_id
  session.rename   → set m.room.name
  session.delete   → unlink from space, leave, forget
  session.archive  → legacy low-priority tag
  plugin.update_check → registrar plugin-version lookup (read-only)
  plugin.update    → run the registrar-supplied install script, then restart

Authorization is control-room membership. The session layer enforces the command
boundary before this handler runs; there is no separate owner/admin role.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlparse

from .registrar_client import RegistrarError
from .rooms import DEFAULT_SESSION_ROOM_NAME

if TYPE_CHECKING:
    from .crypto_driver import CryptoDriver
    from .registrar_client import PluginVersion
    from .rooms import RoomManager
    from .session import MatrixSession

logger = logging.getLogger(__name__)

APP_ID = "@chat4000/hermes-plugin"
INSTALL_SCRIPT_TIMEOUT_S = 300.0
RESTART_DELAY_S = 10.0
DEVICE_PAIR_TTL_SECONDS = 120
PAIR_STATUS_POLL_INTERVAL_S = 1.5


class RegistrarCommandClient(Protocol):
    async def plugin_version(
        self, app_id: str, *, client_id: str | None = None
    ) -> PluginVersion: ...

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


InstallerRunner = Callable[[str], Awaitable[None]]
RestartScheduler = Callable[[], None]


class InstallScriptError(RuntimeError):
    """The registrar-supplied install script could not be fetched or run."""


@dataclass(frozen=True)
class UpdateTarget:
    version: str
    source: str


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
        version: str = "0.0.0",
        registrar: RegistrarCommandClient | None = None,
        client_id: str | None = None,
        installer_runner: InstallerRunner | None = None,
        restart_scheduler: RestartScheduler | None = None,
    ) -> None:
        self._s = session
        self._version = version
        self._registrar = registrar
        self._client_id = client_id
        self._installer_runner = installer_runner or run_install_script
        self._restart_scheduler = restart_scheduler or schedule_gateway_restart
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
                    self._emit_pairing_completed(status)
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

    def _emit_pairing_completed(self, status: dict[str, Any]) -> None:
        """PL4/FLW3-4: the /pair/status completed payload may carry the
        redeeming phone's client_id — emit the machine↔phone join event and
        register the super property (latest pairing wins). The id is absent on
        old registrars / telemetry-off phones; the event still counts the
        completion."""
        from .. import analytics

        props: dict[str, Any] = {"flow": "device_pair"}
        paired_client_id = str(status.get("client_id") or "").strip()
        if paired_client_id:
            analytics.register_paired_client_id(paired_client_id)
            props["paired_client_id"] = paired_client_id
        analytics.track("pairing_completed", props)

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

    async def _update_check(self) -> None:
        target = await self._update_target()
        blockers = self._update_blockers(target)
        needs_update = self._version != target.version
        await self._reply(
            "plugin.update_check",
            {
                "ok": True,
                "current_version": self._version,
                "latest_version": target.version,
                "updatable": needs_update and not blockers,
                "restart_method": "gateway-process",
                "source": target.source,
                "blockers": blockers,
            },
        )

    async def _update(self, content: dict[str, Any]) -> None:
        target = await self._update_target()
        requested = str(content.get("version") or target.version)
        if len(requested) > 32:
            await self._reply("plugin.update", {"ok": False, "error": "version too long"})
            return
        if requested != target.version:
            await self._reply(
                "plugin.update",
                {
                    "ok": False,
                    "error": f"requested version {requested!r} is not the registrar target",
                    "latest_version": target.version,
                },
            )
            return
        blockers = self._update_blockers(target)
        if blockers:
            await self._reply("plugin.update", {"ok": False, "error": "; ".join(blockers)})
            return

        restart_value = content.get("restart", True)
        if not isinstance(restart_value, bool):
            await self._reply("plugin.update", {"ok": False, "error": "restart must be boolean"})
            return
        restart_requested = restart_value
        if self._version == target.version:
            await self._reply(
                "plugin.update",
                {
                    "ok": True,
                    "from_version": self._version,
                    "to_version": target.version,
                    "installed": False,
                    "restart_scheduled": False,
                    "source": target.source,
                },
            )
            return

        from .. import analytics

        # PL2: the self-update is starting — pairs with the registrar's RG2
        # row and the next plugin_started on the new version.
        analytics.track(
            "plugin_upgrading",
            {"from_version": self._version, "to_version": target.version, "trigger": "command"},
        )
        analytics.flush()  # the restart below would otherwise drop the event
        await self._installer_runner(target.source)
        restart_scheduled = False
        if restart_requested:
            self._restart_scheduler()
            restart_scheduled = True
        await self._reply(
            "plugin.update",
            {
                "ok": True,
                "from_version": self._version,
                "to_version": target.version,
                "installed": True,
                "restart_scheduled": restart_scheduled,
                "source": target.source,
            },
        )

    async def _update_target(self) -> UpdateTarget:
        client_id = self._client_id
        if client_id is None:
            from .. import analytics

            # PL3: X-Client-Id = agent_install_id; None when telemetry is off.
            client_id = analytics.machine_client_id()
        result = await self._registrar_client().plugin_version(APP_ID, client_id=client_id)
        return UpdateTarget(version=result.current_version, source=result.source)

    def _registrar_client(self) -> RegistrarCommandClient:
        if self._registrar is not None:
            return self._registrar
        from ..registrar_config import build_registrar_client

        return build_registrar_client()

    def _update_blockers(self, target: UpdateTarget) -> list[str]:
        blockers: list[str] = []
        if not target.version:
            blockers.append("registrar returned an empty current_version")
        if not _is_install_script_source(target.source):
            blockers.append("registrar source is not an http(s) install script URL")
        return blockers

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


def _is_install_script_source(source: str) -> bool:
    parsed = urlparse(source)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _gen_device_pair_code() -> str:
    """A 6-digit CSPRNG OTP (always exactly 6 digits)."""
    return f"{secrets.randbelow(900000) + 100000:06d}"


def _gen_pair_id() -> str:
    return f"p_{secrets.token_hex(6)}"


def _field_error(error: str) -> str:
    return str(error)[:255]


async def run_install_script(source: str) -> None:
    """Fetch and run the registrar-supplied install script non-interactively."""
    await asyncio.to_thread(_run_install_script_sync, source)


def _run_install_script_sync(source: str) -> None:
    if not _is_install_script_source(source):
        raise InstallScriptError("registrar source is not an http(s) install script URL")
    script_path = ""
    try:
        with urllib.request.urlopen(source, timeout=30.0) as resp:  # noqa: S310
            script = resp.read()
        fd, script_path = tempfile.mkstemp(
            prefix="chat4000-plugin-update-", suffix=".sh", dir="/tmp"
        )
        with os.fdopen(fd, "wb") as f:
            f.write(script)
        os.chmod(script_path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        args = ["/bin/bash", script_path, "--no-wizard"]
        from ..registrar_config import resolve_env

        if resolve_env() == "stage":
            args.append("--stage")
        result = subprocess.run(  # noqa: S603  # trusted argv; script source is registrar policy
            args,
            capture_output=True,
            text=True,
            timeout=INSTALL_SCRIPT_TIMEOUT_S,
            check=False,
        )
    except (urllib.error.URLError, OSError, TimeoutError, subprocess.TimeoutExpired) as exc:
        raise InstallScriptError(
            f"install script failed before completion: {type(exc).__name__}"
        ) from exc
    finally:
        if script_path:
            with contextlib.suppress(OSError):
                os.unlink(script_path)
    if result.returncode != 0:
        raise InstallScriptError(f"install script exited {result.returncode}")


def schedule_gateway_restart() -> None:
    """Schedule a detached gateway restart after the command result can be sent."""
    helper = (
        "import shutil, subprocess, time\n"
        f"time.sleep({RESTART_DELAY_S!r})\n"
        "subprocess.run(['pkill', '-9', '-f', 'hermes gateway run'], check=False)\n"
        "time.sleep(2.0)\n"
        "running = subprocess.run(['pgrep', '-f', 'hermes gateway run'], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False).returncode == 0\n"
        "hermes = shutil.which('hermes')\n"
        "if not running and hermes:\n"
        "    log = open('/tmp/gateway.log', 'ab')\n"
        "    subprocess.Popen([hermes, 'gateway', 'run'], stdout=log, stderr=subprocess.STDOUT, "
        "start_new_session=True, close_fds=True)\n"
    )
    subprocess.Popen(  # noqa: S603  # trusted fixed argv; detached restart helper
        [sys.executable, "-c", helper],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
