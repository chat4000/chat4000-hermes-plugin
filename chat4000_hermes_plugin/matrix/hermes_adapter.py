"""v2 Hermes adapter — wires MatrixSession into Hermes' BasePlatformAdapter.

This is the capstone that replaces the v1 `adapter.py` internals: instead of the
relay transport + group-key crypto, it drives a `MatrixSession` (gateway +
OlmMachine binding + rooms). The Hermes integration scaffolding (dynamic base
class, register_platform, build_source/MessageEvent/handle_message) is unchanged
from v1 — only the transport beneath it is swapped.

  inbound user message (session room) → handle_message → Hermes agent
  inbound chat4000.command (control)   → CommandHandler
  agent reply (streaming)              → TurnWriter (anchor + m.replace edits)
  agent tool calls                     → chat4000.tool events
  agent reasoning/typing               → chat4000.status state

⚠️ Hermes-runtime-coupled: the reply-pipeline callback names/shapes mirror the v1
adapter's `reply_pipeline_options` (which itself documents them as Hermes-version
dependent). Verify against the target Hermes when wiring live; the mapping to
TurnWriter is the stable part. Not unit-tested offline (needs the Hermes process
+ the built pyvodozemac wheel).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from ..package_info import read_package_version
from .commands import CommandHandler
from .creds_store import load_bot_creds
from .session import MatrixSession

logger = logging.getLogger(__name__)

# Throttle streaming edits to stay well under the 900 msg/min cap (X5): at most
# one edit per this interval; the final edit always flushes immediately.
STREAM_MIN_INTERVAL_S = 0.4


@dataclass
class _TurnState:
    """Per-room reply-in-progress state."""

    room_id: str
    anchor_id: Optional[str] = None
    last_text: str = ""
    last_edit_at: float = 0.0
    tool_events: dict[str, dict] = field(default_factory=dict)  # tool_id → {event_id, meta}


class Chat4000MatrixAdapter:
    """Bound at register() time to BasePlatformAdapter (see v1 _make_adapter_class)."""

    def __init__(self, config, **kwargs):
        from gateway.platforms.base import BasePlatformAdapter  # type: ignore[import-not-found]
        from gateway.config import Platform  # type: ignore[import-not-found]

        BasePlatformAdapter.__init__(self, config=config, platform=Platform("chat4000"))
        extra = getattr(config, "extra", {}) or {}
        self._account_id = extra.get("accountId") or extra.get("account_id") or "default"
        self._session: Optional[MatrixSession] = None
        self._commands: Optional[CommandHandler] = None
        self._turns: dict[str, _TurnState] = {}
        self._active_room: Optional[str] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._connected = False

    @property
    def name(self) -> str:
        return "chat4000"

    # ─── lifecycle ────────────────────────────────────────────────────────

    async def connect(self) -> bool:
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

        creds = load_bot_creds(self._account_id)
        if creds is None:
            logger.error("chat4000 v2: no bot creds — run `chat4000 pair` to onboard")
            return False

        self._session = MatrixSession(
            creds,
            account_id=self._account_id,
            plugin_version=read_package_version(),
            on_user_message=self._on_user_message,
            on_command=self._on_command,
        )
        self._commands = CommandHandler(self._session, version=read_package_version())

        try:
            await self._session.start()
            await self._session.ensure_bootstrap()
            # Pick up everyone who paired (CLI recorded them) — invite + share keys.
            from .users_store import load_known_users
            users = load_known_users(self._account_id)
            if users:
                await self._session.set_members(users)
                for uid in users:
                    await self._session.invite_user(uid)
        except Exception as exc:  # noqa: BLE001
            logger.error("chat4000 v2 connect failed: %s", exc)
            return False

        self._connected = True
        from .. import analytics
        analytics.track("gateway_started", {"transport": "matrix"})
        return True

    async def disconnect(self) -> None:
        self._connected = False
        if self._session is not None:
            await self._session.stop()
            self._session = None
        from .. import analytics
        analytics.track("gateway_stopped", {})
        analytics.flush()

    # ─── inbound callbacks (from MatrixSession) ───────────────────────────

    async def _on_command(self, room_id: str, command: str, content: dict) -> None:
        if self._commands is not None:
            await self._commands.handle(room_id, command, content)

    async def _on_user_message(self, room_id: str, sender: str, content: dict) -> None:
        """A user message in a session room → dispatch to Hermes. `_active_room`
        is set so the reply-pipeline callbacks target the right room."""
        from gateway.platforms.base import (  # type: ignore[import-not-found]
            MessageEvent,
            MessageType,
        )

        msgtype = content.get("msgtype")
        text = content.get("body", "") if msgtype == "m.text" else ""
        # media (m.image/m.audio) → P6 (download + decrypt via the HTTP media path)
        message_type = MessageType.TEXT

        source = self.build_source(chat_id=room_id, user_id=sender, chat_type="dm")
        event = MessageEvent(
            text=text,
            message_type=message_type,
            source=source,
            raw_message=content,
            message_id=uuid.uuid4().hex,
            media_urls=[],
            media_types=[],
        )

        self._active_room = room_id
        try:
            await self.handle_message(event)
        finally:
            self._active_room = None

    # ─── outbound: oneshot send + typing ──────────────────────────────────

    async def send(self, chat_id: str, content, *, reply_to=None, metadata=None):
        from gateway.platforms.base import SendResult  # type: ignore[import-not-found]
        if self._session is None:
            return SendResult(success=False, error="not connected")
        text = content if isinstance(content, str) else (content or {}).get("text", "")
        if not text:
            return SendResult(success=True, message_id="")
        # Oneshot (non-streaming) reply: a one-edit turn.
        tw = self._session.turn_writer(chat_id)
        anchor = await tw.start_turn(chat_id)
        if anchor:
            await tw.stream_edit(chat_id, anchor, str(text), final=True)
        await tw.set_status(chat_id, "idle")
        return SendResult(success=True, message_id=anchor or "")

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        if self._session is not None:
            await self._session.turn_writer(chat_id).set_status(chat_id, "typing")

    async def get_chat_info(self, chat_id) -> dict:
        return {"name": f"chat4000 ({chat_id[:8]}…)", "type": "dm", "chat_id": chat_id}

    # ─── streaming reply pipeline (Hermes-contract) ───────────────────────

    def reply_pipeline_options(self) -> dict:
        """Map Hermes' per-turn reply events to TurnWriter on `_active_room`."""
        if self._session is None:
            return {}

        def _tw(room_id: str):
            return self._session.turn_writer(room_id)  # type: ignore[union-attr]

        def _turn(room_id: str) -> _TurnState:
            st = self._turns.get(room_id)
            if st is None:
                st = _TurnState(room_id=room_id)
                self._turns[room_id] = st
            return st

        async def on_reasoning_stream(_p: dict) -> None:
            if self._active_room:
                await _tw(self._active_room).set_status(self._active_room, "thinking")

        async def on_assistant_message_start(_p: dict) -> None:
            room = self._active_room
            if not room:
                return
            st = _turn(room)
            if st.anchor_id is None:
                st.anchor_id = await _tw(room).start_turn(room)
            await _tw(room).set_status(room, "typing")

        async def on_partial_reply(payload: dict) -> None:
            room = self._active_room
            text = (payload or {}).get("text") or ""
            if not room or not text:
                return
            st = _turn(room)
            if st.anchor_id is None:
                st.anchor_id = await _tw(room).start_turn(room)
            st.last_text = text
            now = (self._loop.time() if self._loop else 0.0)
            if now - st.last_edit_at >= STREAM_MIN_INTERVAL_S:
                st.last_edit_at = now
                await _tw(room).stream_edit(room, st.anchor_id, text, final=False)

        async def on_tool_start(name: str, args) -> str:
            room = self._active_room
            if not room:
                return ""
            st = _turn(room)
            if st.anchor_id is None:
                st.anchor_id = await _tw(room).start_turn(room)
            tool_id = uuid.uuid4().hex
            args_str = args if isinstance(args, str) else _json(args)
            ev = await _tw(room).tool_start(
                room, st.anchor_id, tool_id=tool_id, name=name, args=args_str
            )
            st.tool_events[tool_id] = {"event_id": ev, "name": name, "args": args_str}
            await _tw(room).set_status(room, "working")
            return tool_id

        async def on_tool_end(tool_id: str, *, status: str = "done", result: str = "") -> None:
            room = self._active_room
            if not room:
                return
            st = _turn(room)
            meta = st.tool_events.pop(tool_id, None)
            if not meta or not meta.get("event_id"):
                return
            await _tw(room).tool_end(
                room,
                meta["event_id"],
                tool_id=tool_id,
                name=meta["name"],
                args=meta["args"],
                status=status,
                result=result,
                duration_ms=0,
            )

        async def on_final(payload: dict) -> None:
            room = self._active_room
            if not room:
                return
            st = self._turns.pop(room, None)
            text = (payload or {}).get("text") or (st.last_text if st else "")
            tw = _tw(room)
            if st and st.anchor_id:
                await tw.stream_edit(room, st.anchor_id, text, final=True)
            elif text:
                anchor = await tw.start_turn(room)
                if anchor:
                    await tw.stream_edit(room, anchor, text, final=True)
            await tw.set_status(room, "idle")

        return {
            "on_reasoning_stream": on_reasoning_stream,
            "on_assistant_message_start": on_assistant_message_start,
            "on_partial_reply": on_partial_reply,
            "on_tool_start": on_tool_start,
            "on_tool_end": on_tool_end,
            "on_final": on_final,
        }


def _json(v: Any) -> str:
    import json
    try:
        return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return str(v)
