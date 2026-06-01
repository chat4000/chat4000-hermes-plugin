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
from typing import Optional

logger = logging.getLogger(__name__)


class CommandHandler:
    def __init__(self, session, *, owner_user_id: Optional[str] = None, version: str = "0.0.0"):
        self._s = session
        self._owner = owner_user_id
        self._version = version

    async def handle(self, room_id: str, command: str, content: dict) -> None:
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
            logger.warning("command %s failed: %s", command, exc)
            await self._reply(command, {"ok": False, "error": str(exc)})

    # ─── handlers ─────────────────────────────────────────────────────────

    async def _session_new(self, content: dict) -> None:
        title = content.get("title") or "session"
        agent_id = content.get("agent_id") or "main"
        room_id = await self._s.rooms.create_session_room(title, agent_id)
        for uid in self._s.members:
            await self._s.rooms.invite_user(room_id, uid)
        await self._reply("session.new", {"ok": True, "room_id": room_id})

    async def _session_rename(self, content: dict) -> None:
        room_id = content.get("room_id")
        title = content.get("title")
        if not room_id or not title:
            await self._reply("session.rename", {"ok": False, "error": "room_id and title required"})
            return
        await self._s.rooms.rename_session(room_id, title)
        await self._reply("session.rename", {"ok": True, "room_id": room_id})

    async def _session_archive(self, content: dict) -> None:
        room_id = content.get("room_id")
        if not room_id:
            await self._reply("session.archive", {"ok": False, "error": "room_id required"})
            return
        await self._s.rooms.archive_session(room_id)
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

    async def _update(self, content: dict) -> None:
        # Owner-gated remote code update over chat. Deferred until the registrar
        # defines owner establishment/proof (X4). Refuse cleanly.
        await self._reply(
            "plugin.update",
            {"ok": False, "error": "plugin.update is not supported yet (owner model undefined)"},
        )

    # ─── reply ────────────────────────────────────────────────────────────

    async def _reply(self, command: str, fields: dict) -> None:
        # Coarse, content-free: which command ran and whether it succeeded.
        try:
            from .. import analytics
            analytics.track("command_handled", {"command": command, "ok": fields.get("ok")})
        except Exception:
            pass
        control = self._s.rooms.control_room_id
        if control is None:
            logger.warning("no control room; cannot reply to %s", command)
            return
        content = {"msgtype": "chat4000.command_result", "command": command, **fields}
        await self._s.crypto.send_room_event(
            control, "m.room.message", content, self._s.members, push=False
        )
