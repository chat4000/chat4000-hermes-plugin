"""Turns, streaming, tool events, and status (protocol E — "Turns & anchoring").

One agent reply = one **turn**, anchored by a single `m.room.message` event that
edits itself via `m.replace` as text streams; the final edit carries the full
answer and is the ONLY event with `chat4000.push: true`. Tool calls are separate
`chat4000.tool` events related to the anchor via `m.relates_to:{rel_type:
"chat4000.turn"}`. Live activity is a cleartext `chat4000.status` state event.

Push discipline (the rule that must not be gotten wrong): EVERY event of the turn
carries `chat4000.push: false` — the anchor, every streaming edit, every tool
start/edit — EXCEPT the single final answer edit (`true`). The first message of
the turn MUST be explicitly `false` (absent ⇒ push-eligible ⇒ would wake on the
opening partial).

Encrypted events go through the crypto driver; the status state event is
cleartext (state events aren't E2EE in Matrix — accepted, it carries only an
activity word).
"""

from __future__ import annotations

from .crypto_driver import CryptoDriver
from .gateway_client import GatewayClient

TOOL_MSGTYPE = "chat4000.tool"
STATUS_STATE = "chat4000.status"


class TurnWriter:
    """One per room. Holds nothing durable — the adapter threads anchor/tool ids."""

    def __init__(self, crypto: CryptoDriver, gateway: GatewayClient, members: list[str]) -> None:
        self._c = crypto
        self._gw = gateway
        self._members = members

    # ─── the answer anchor ────────────────────────────────────────────────

    async def start_turn(self, room_id: str) -> str | None:
        """Post the anchor (empty answer, push:false). Returns its event_id."""
        return await self._c.send_room_event(
            room_id,
            "m.room.message",
            {"msgtype": "m.text", "body": ""},
            self._members,
            push=False,
        )

    async def stream_edit(
        self, room_id: str, anchor_id: str, text: str, *, final: bool
    ) -> str | None:
        """Edit the anchor with the latest text. `final=True` is the one event
        that wakes the user (`push:true`)."""
        content = {
            "msgtype": "m.text",
            "body": "* " + text,  # fallback body for non-edit-aware clients
            "m.new_content": {"msgtype": "m.text", "body": text},
        }
        return await self._c.send_room_event(
            room_id,
            "m.room.message",
            content,
            self._members,
            push=final,
            relates_to={"rel_type": "m.replace", "event_id": anchor_id},
        )

    # ─── tool events (two sends per tool) ─────────────────────────────────

    async def tool_start(
        self, room_id: str, anchor_id: str, *, tool_id: str, name: str, args: str, icon: str = ""
    ) -> str | None:
        tool = {
            "tool_id": tool_id,
            "name": name,
            "icon": icon,
            "args": args,
            "status": "running",
            "result": "",
            "duration_ms": 0,
        }
        # The turn link is chat4000.turn_id INSIDE the encrypted content (protocol
        # E / client contract) — NOT a cleartext m.relates_to. tool_end is the one
        # that uses m.relates_to (an m.replace edit of this event).
        return await self._c.send_room_event(
            room_id,
            TOOL_MSGTYPE,
            {"msgtype": TOOL_MSGTYPE, TOOL_MSGTYPE: tool, "chat4000.turn_id": anchor_id},
            self._members,
            push=False,
        )

    async def tool_end(
        self,
        room_id: str,
        tool_event_id: str,
        *,
        tool_id: str,
        name: str,
        args: str,
        status: str,
        result: str,
        duration_ms: int,
        icon: str = "",
    ) -> str | None:
        tool = {
            "tool_id": tool_id,
            "name": name,
            "icon": icon,
            "args": args,
            "status": status,  # done | failed
            "result": result,
            "duration_ms": duration_ms,
        }
        content = {
            "msgtype": TOOL_MSGTYPE,
            TOOL_MSGTYPE: tool,
            "m.new_content": {"msgtype": TOOL_MSGTYPE, TOOL_MSGTYPE: tool},
        }
        return await self._c.send_room_event(
            room_id,
            TOOL_MSGTYPE,
            content,
            self._members,
            push=False,
            relates_to={"rel_type": "m.replace", "event_id": tool_event_id},
        )

    # ─── status (cleartext state event) ───────────────────────────────────

    async def set_status(self, room_id: str, state: str) -> None:
        """thinking | working | typing | idle. Cleartext state event — overwrites
        the single `chat4000.status` (state_key "")."""
        await self._gw.request(
            "PUT",
            f"/_matrix/client/v3/rooms/{room_id}/state/{STATUS_STATE}/",
            {"state": state},
        )
