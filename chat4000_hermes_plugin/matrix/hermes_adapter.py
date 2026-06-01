"""v2 Hermes adapter — wires MatrixSession into Hermes' BasePlatformAdapter.

Replaces the v1 relay/group-key internals with a `MatrixSession` (gateway +
OlmMachine binding + rooms). Hermes integration scaffolding (dynamic base class,
build_source/MessageEvent/handle_message) is unchanged from v1.

  inbound user message (session room) → handle_message → Hermes agent
  inbound chat4000.command (control)   → CommandHandler
  agent reply (streaming)              → TurnWriter (anchor + m.replace edits)
  agent reasoning/typing               → chat4000.status state
  agent tool calls                     → chat4000.tool events, via plugin_hooks
                                         (pre/post_tool_call → external_tool_*)

Text streaming + status flow through `reply_pipeline_options`. Tool calls flow
through `plugin_hooks` (Hermes' standard runner fires pre/post_tool_call but not
the reply-pipeline tool callbacks — same reason as v1), which calls the
`external_tool_*` methods here.

⚠️ Hermes-runtime-coupled; not unit-tested offline (needs the Hermes process +
the built pyvodozemac wheel).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from ..package_info import read_package_version
from .commands import CommandHandler
from .creds_store import load_bot_creds
from .session import MatrixSession

logger = logging.getLogger(__name__)

# Throttle streaming edits to stay under the 900 msg/min cap (X5); the final edit
# always flushes immediately.
STREAM_MIN_INTERVAL_S = 0.4


@dataclass
class _TurnState:
    room_id: str
    anchor_id: Optional[str] = None
    last_text: str = ""
    last_edit_at: float = 0.0


class Chat4000MatrixAdapter:
    """Bound at register() time to BasePlatformAdapter (see adapter._make_adapter_class)."""

    def __init__(self, config, **kwargs):
        from gateway.platforms.base import BasePlatformAdapter  # type: ignore[import-not-found]
        from gateway.config import Platform  # type: ignore[import-not-found]

        BasePlatformAdapter.__init__(self, config=config, platform=Platform("chat4000"))
        extra = getattr(config, "extra", {}) or {}
        self._account_id = extra.get("accountId") or extra.get("account_id") or "default"
        self._session: Optional[MatrixSession] = None
        self._commands: Optional[CommandHandler] = None
        self._turns: dict[str, _TurnState] = {}
        # tool_id → (room_id, {name, args, event_id}) for hook-driven tool events.
        self._tools: dict[str, tuple[str, dict]] = {}
        self._active_room: Optional[str] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._media = None  # MediaClient, built at connect
        self._connected = False

        # Make this adapter discoverable by plugin_hooks (tool bubbles).
        from ..plugin_hooks import register_active_adapter
        register_active_adapter(self)

    @property
    def name(self) -> str:
        return "chat4000"

    # ─── lifecycle ────────────────────────────────────────────────────────

    async def connect(self) -> bool:
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

        from .. import analytics

        creds = load_bot_creds(self._account_id)
        if creds is None:
            logger.error("chat4000 v2: no bot creds — run `chat4000 pair` to onboard")
            analytics.track("gateway_connect_failed", {"reason": "no_creds"})
            return False

        self._session = MatrixSession(
            creds,
            account_id=self._account_id,
            plugin_version=read_package_version(),
            on_user_message=self._on_user_message,
            on_command=self._on_command,
        )
        self._commands = CommandHandler(self._session, version=read_package_version())

        from .media import MediaClient, media_base_from_gateway
        self._media = MediaClient(media_base_from_gateway(creds.gateway_url), creds.access_token)

        try:
            await self._session.start()
            await self._session.ensure_bootstrap()
            from .users_store import load_known_users
            users = load_known_users(self._account_id)
            if users:
                await self._session.set_members(users)
                for uid in users:
                    await self._session.invite_user(uid)
        except Exception as exc:  # noqa: BLE001
            logger.error("chat4000 v2 connect failed: %s", exc)
            analytics.track("gateway_connect_failed", {"reason": type(exc).__name__})
            return False

        self._connected = True
        analytics.track("gateway_started", {"transport": "matrix"})
        return True

    async def disconnect(self) -> None:
        self._connected = False
        from ..plugin_hooks import deregister_active_adapter
        deregister_active_adapter(self)
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
        from gateway.platforms.base import (  # type: ignore[import-not-found]
            MessageEvent,
            MessageType,
        )

        msgtype = content.get("msgtype")
        text = content.get("body", "") if msgtype == "m.text" else ""
        message_type = MessageType.TEXT
        media_urls: list[str] = []
        media_types: list[str] = []

        # Encrypted attachments (D.3): the `file` object is cleartext now that the
        # event is decrypted — download the ciphertext, decrypt, cache for Hermes'
        # vision/STT tools (path passed via media_urls, same as Telegram/WhatsApp).
        if msgtype in ("m.image", "m.audio", "m.video", "m.file") and self._media is not None:
            file_meta = content.get("file")
            if file_meta:
                try:
                    raw = await self._media.download_attachment(file_meta)
                    mime = (content.get("info") or {}).get("mimetype") or "application/octet-stream"
                    ext = "." + (mime.rsplit("/", 1)[-1] or "bin").split(";")[0].strip()
                    from gateway.platforms.base import (  # type: ignore[import-not-found]
                        cache_audio_from_bytes,
                        cache_image_from_bytes,
                    )
                    if msgtype == "m.image":
                        media_urls.append(cache_image_from_bytes(raw, ext=".jpg" if ext == ".jpeg" else ext))
                        message_type = MessageType.IMAGE
                    elif msgtype == "m.audio":
                        media_urls.append(cache_audio_from_bytes(raw, ext=ext))
                        message_type = MessageType.AUDIO
                    media_types.append(mime)
                    text = content.get("body", "") if msgtype not in ("m.image", "m.audio") else ""
                except Exception as exc:  # noqa: BLE001
                    logger.warning("chat4000: inbound media decrypt failed: %s", exc)

        source = self.build_source(chat_id=room_id, user_id=sender, chat_type="dm")
        event = MessageEvent(
            text=text,
            message_type=message_type,
            source=source,
            raw_message=content,
            message_id=uuid.uuid4().hex,
            media_urls=media_urls,
            media_types=media_types,
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
        tw = self._tw(chat_id)
        anchor = await tw.start_turn(chat_id)
        if anchor:
            await tw.stream_edit(chat_id, anchor, str(text), final=True)
        await tw.set_status(chat_id, "idle")
        return SendResult(success=True, message_id=anchor or "")

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        if self._session is not None:
            await self._tw(chat_id).set_status(chat_id, "typing")

    async def get_chat_info(self, chat_id) -> dict:
        return {"name": f"chat4000 ({chat_id[:8]}…)", "type": "dm", "chat_id": chat_id}

    # ─── tool events (called from plugin_hooks) ───────────────────────────

    async def external_tool_start(self, name: str, args: Any, icon: str = "") -> str:
        """A tool began (from pre_tool_call). Emit a chat4000.tool event related
        to the active turn; returns a tool_id to correlate the end."""
        room = self._active_room
        if not room or self._session is None:
            return ""
        anchor = await self._ensure_anchor(room)
        tool_id = uuid.uuid4().hex
        args_str = args if isinstance(args, str) else _json(args)
        ev = await self._tw(room).tool_start(
            room, anchor, tool_id=tool_id, name=name, args=args_str, icon=icon
        )
        self._tools[tool_id] = (room, {"name": name, "args": args_str, "event_id": ev})
        await self._tw(room).set_status(room, "working")
        return tool_id

    async def external_tool_end(self, tool_id: str, *, status: str = "done", result: str = "") -> None:
        """A tool finished (from post_tool_call). Edit its event to done/failed."""
        entry = self._tools.pop(tool_id, None)
        if entry is None or self._session is None:
            return
        room, meta = entry
        if not meta.get("event_id"):
            return
        await self._tw(room).tool_end(
            room, meta["event_id"], tool_id=tool_id, name=meta["name"],
            args=meta["args"], status=status, result=result, duration_ms=0,
        )

    # ─── streaming reply pipeline (text + status; Hermes-contract) ────────

    def reply_pipeline_options(self) -> dict:
        if self._session is None:
            return {}

        async def on_reasoning_stream(_p: dict) -> None:
            if self._active_room:
                await self._tw(self._active_room).set_status(self._active_room, "thinking")

        async def on_assistant_message_start(_p: dict) -> None:
            if self._active_room:
                await self._ensure_anchor(self._active_room)
                await self._tw(self._active_room).set_status(self._active_room, "typing")

        async def on_partial_reply(payload: dict) -> None:
            room = self._active_room
            text = (payload or {}).get("text") or ""
            if not room or not text:
                return
            anchor = await self._ensure_anchor(room)
            st = self._turn_state(room)
            st.last_text = text
            now = self._loop.time() if self._loop else 0.0
            if now - st.last_edit_at >= STREAM_MIN_INTERVAL_S:
                st.last_edit_at = now
                await self._tw(room).stream_edit(room, anchor, text, final=False)

        async def on_final(payload: dict) -> None:
            room = self._active_room
            if not room:
                return
            st = self._turns.pop(room, None)
            text = (payload or {}).get("text") or (st.last_text if st else "")
            tw = self._tw(room)
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
            "on_final": on_final,
        }

    # ─── helpers ──────────────────────────────────────────────────────────

    def _tw(self, room_id: str):
        return self._session.turn_writer(room_id)  # type: ignore[union-attr]

    def _turn_state(self, room_id: str) -> _TurnState:
        st = self._turns.get(room_id)
        if st is None:
            st = _TurnState(room_id=room_id)
            self._turns[room_id] = st
        return st

    async def _ensure_anchor(self, room_id: str) -> Optional[str]:
        st = self._turn_state(room_id)
        if st.anchor_id is None:
            st.anchor_id = await self._tw(room_id).start_turn(room_id)
        return st.anchor_id


def _json(v: Any) -> str:
    try:
        return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return str(v)
