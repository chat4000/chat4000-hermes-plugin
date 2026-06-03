"""Room + space management (protocol E).

The plugin's Matrix structure:
  - one `m.space` (the plugin's space)
  - exactly one `control` room (commands), encrypted, `chat4000.room_kind=control`
  - N `session` rooms, encrypted, `chat4000.room_kind=session`

All chat4000 rooms are E2E-encrypted (`m.room.encryption` set on creation). The
space itself is structural (not encrypted) — its rooms are `m.space.child`.

These are plain C-S calls over the gateway `req` channel (createRoom, state
events, invite). Sending *messages* into the rooms is the crypto driver's job.

State (space_id, control_room_id) is owned by the caller (adapter) and persisted
via the binding store / a small state file — this manager is given them or
discovers them from sync.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .gateway_client import GatewayClient

logger = logging.getLogger(__name__)

MEGOLM = "m.megolm.v1.aes-sha2"
ROOM_KIND = "chat4000.room_kind"


@dataclass
class RoomManager:
    gateway: GatewayClient
    server_name: str  # for m.space.child `via`
    space_id: str | None = None
    control_room_id: str | None = None

    # ─── creation ─────────────────────────────────────────────────────────

    async def create_space(self, name: str = "chat4000") -> str:
        status, body = await self.gateway.request(
            "POST",
            "/_matrix/client/v3/createRoom",
            {
                "name": name,
                "preset": "private_chat",
                "creation_content": {"type": "m.space"},
            },
        )
        self._check(status, body, "create_space")
        self.space_id = body["room_id"]
        return self.space_id

    async def create_control_room(self, name: str = "Commands") -> str:
        rid = await self._create_encrypted_room(name, kind="control")
        self.control_room_id = rid
        return rid

    async def create_session_room(self, title: str, agent_id: str = "main") -> str:
        # agent_id is carried in the room_kind content so the plugin can route
        # the room to the right Hermes agent session.
        return await self._create_encrypted_room(
            title or "session", kind="session", extra_kind={"agent_id": agent_id}
        )

    async def _create_encrypted_room(
        self, name: str, *, kind: str, extra_kind: dict[str, Any] | None = None
    ) -> str:
        kind_content = {"kind": kind}
        if extra_kind:
            kind_content.update(extra_kind)
        status, body = await self.gateway.request(
            "POST",
            "/_matrix/client/v3/createRoom",
            {
                "name": name,
                "preset": "private_chat",
                "initial_state": [
                    {
                        "type": "m.room.encryption",
                        "state_key": "",
                        "content": {"algorithm": MEGOLM},
                    },
                    {"type": ROOM_KIND, "state_key": "", "content": kind_content},
                ],
            },
        )
        self._check(status, body, f"create_{kind}_room")
        room_id: str = body["room_id"]
        logger.debug(
            "created %s room id=%s (encryption + room_kind set in initial_state)", kind, room_id
        )
        if self.space_id:
            await self._add_space_child(room_id)
        return room_id

    # ─── membership ───────────────────────────────────────────────────────

    async def invite_user(self, room_id: str, user_id: str) -> None:
        logger.debug("inviting user=%s to room=%s", user_id, room_id)
        status, body = await self.gateway.request(
            "POST", f"/_matrix/client/v3/rooms/{room_id}/invite", {"user_id": user_id}
        )
        # 200 on success; an already-joined user yields M_FORBIDDEN which we treat
        # as benign.
        if status >= 400 and body.get("errcode") not in ("M_FORBIDDEN",):
            logger.warning("invite %s to %s failed: %s %s", user_id, room_id, status, body)

    async def invite_to_all(self, user_id: str, session_rooms: list[str]) -> None:
        """On pairing completion: invite the user to space + control + sessions."""
        targets = [r for r in (self.space_id, self.control_room_id, *session_rooms) if r]
        for r in targets:
            await self.invite_user(r, user_id)

    # ─── session lifecycle (commands) ─────────────────────────────────────

    async def rename_session(self, room_id: str, title: str) -> None:
        await self._set_state(room_id, "m.room.name", "", {"name": title})

    async def archive_session(self, room_id: str) -> None:
        # No native Matrix "archive" — tag low-priority (non-destructive). See
        # pushback G10: archive semantics are unspecified; this is our choice.
        await self.gateway.request(
            "PUT",
            f"/_matrix/client/v3/user/{self.gateway.user_id}/rooms/{room_id}/tags/m.lowpriority",
            {"order": 0.0},
        )

    # ─── discovery (deterministic, from the homeserver) ───────────────────

    async def discover(self) -> None:
        """Find our existing space + control room by asking the homeserver, so a
        gateway restart doesn't create DUPLICATE rooms. Lists the bot's joined
        rooms and reads each one's `m.room.create` (is it the space?) and
        `chat4000.room_kind` (is it the control room?)."""
        status, body = await self.gateway.request("GET", "/_matrix/client/v3/joined_rooms")
        if status >= 400:
            logger.warning("discover: joined_rooms failed: %s", status)
            return
        for room_id in body.get("joined_rooms", []):
            if self.space_id is None:
                s, c = await self.gateway.request(
                    "GET", f"/_matrix/client/v3/rooms/{room_id}/state/m.room.create/"
                )
                if s < 400 and (c or {}).get("type") == "m.space":
                    self.space_id = room_id
                    continue
            if self.control_room_id is None:
                s, k = await self.gateway.request(
                    "GET", f"/_matrix/client/v3/rooms/{room_id}/state/{ROOM_KIND}/"
                )
                if s < 400 and (k or {}).get("kind") == "control":
                    self.control_room_id = room_id

    # ─── discovery (from sync) ────────────────────────────────────────────

    def classify_room(self, room_id: str, required_state: list[dict[str, Any]]) -> str | None:
        """Read `chat4000.room_kind` out of a synced room's required_state and
        record the control room. Returns the kind, or None if unmarked."""
        for ev in required_state:
            if ev.get("type") == ROOM_KIND and ev.get("state_key", "") == "":
                kind = (ev.get("content") or {}).get("kind")
                if kind == "control":
                    self.control_room_id = room_id
                return kind
        return None

    # ─── internals ────────────────────────────────────────────────────────

    async def _add_space_child(self, child_room_id: str) -> None:
        await self._set_state(
            self.space_id,  # type: ignore[arg-type]
            "m.space.child",
            child_room_id,
            {"via": [self.server_name]},
        )

    async def _set_state(
        self, room_id: str, etype: str, state_key: str, content: dict[str, Any]
    ) -> None:
        status, body = await self.gateway.request(
            "PUT",
            f"/_matrix/client/v3/rooms/{room_id}/state/{etype}/{state_key}",
            content,
        )
        if status >= 400:
            logger.warning(
                "set_state %s/%s in %s failed: %s %s", etype, state_key, room_id, status, body
            )

    @staticmethod
    def _check(status: int, body: dict[str, Any], what: str) -> None:
        if status >= 400 or "room_id" not in body:
            raise RuntimeError(f"{what} failed: {status} {body}")
