"""v2 Hermes adapter — wires MatrixSession into Hermes' BasePlatformAdapter.

Replaces the v1 relay/group-key internals with a `MatrixSession` (gateway +
OlmMachine binding + rooms). Hermes integration scaffolding (dynamic base class,
build_source/MessageEvent/handle_message) is unchanged from v1.

  inbound user message (session room) → handle_message → Hermes agent
  inbound chat4000.command (control)   → CommandHandler
  agent reply (streaming)              → TurnWriter (anchor + m.replace edits)
  agent live activity                  → native m.typing (on/off)
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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..package_info import read_package_version
from .commands import CommandHandler
from .creds_store import load_bot_creds
from .session import MatrixSession
from .turns import TurnWriter

if TYPE_CHECKING:
    from .media import MediaClient

logger = logging.getLogger(__name__)

# Throttle streaming edits to stay under the 900 msg/min cap (X5); the final edit
# always flushes immediately.
STREAM_MIN_INTERVAL_S = 0.4

# Native m.typing: signal on with this timeout, re-PUT before it lapses while the
# turn is still active. (protocol 67919b9 — chat4000.status removed.)
TYPING_TIMEOUT_MS = 30000
TYPING_REFRESH_S = 20.0


@dataclass
class _TurnState:
    room_id: str
    anchor_id: str | None = None
    last_text: str = ""
    last_edit_at: float = 0.0


class Chat4000MatrixAdapter:
    """Bound at register() time to BasePlatformAdapter (see adapter._make_adapter_class)."""

    def __init__(self, config: Any, **kwargs: Any) -> None:  # noqa: ANN401  # Hermes host config/kwargs (untyped host objects)
        from gateway.config import Platform
        from gateway.platforms.base import BasePlatformAdapter

        BasePlatformAdapter.__init__(self, config=config, platform=Platform("chat4000"))
        extra = getattr(config, "extra", {}) or {}
        self._account_id = extra.get("accountId") or extra.get("account_id") or "default"
        self._session: MatrixSession | None = None
        self._commands: CommandHandler | None = None
        self._turns: dict[str, _TurnState] = {}
        # tool_id → (room_id, {name, args, event_id}) for hook-driven tool events.
        self._tools: dict[str, tuple[str, dict[str, Any]]] = {}
        self._active_room: str | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._media: MediaClient | None = None  # built at connect
        self._connected = False
        # Rooms with an in-flight turn — the ONLY rooms send_typing is allowed to
        # keep typing alive. Cleared the moment a turn ends so Hermes' _keep_typing
        # loop can't re-light native typing after the answer is delivered.
        self._typing_on: set[str] = set()
        # room_id → monotonic time of the last typing:true PUT (presence = typing
        # currently on). Used to throttle refreshes inside the 30s typing timeout.
        self._typing_at: dict[str, float] = {}

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
            # connect() boundary: report the unexpected failure once, then convert
            # to a False return so Hermes can surface "not connected" cleanly.
            from ..error_log import dump_chat4000_trace

            logger.error("chat4000 v2 connect failed: %s", exc)
            analytics.track("gateway_connect_failed", {"reason": type(exc).__name__})
            dump_chat4000_trace("matrix.connect", exc)
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

    async def _on_command(self, room_id: str, command: str, content: dict[str, Any]) -> None:
        if self._commands is not None:
            await self._commands.handle(room_id, command, content)

    async def _on_user_message(self, room_id: str, sender: str, content: dict[str, Any]) -> None:
        from gateway.platforms.base import (
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
                    from gateway.platforms.base import (
                        cache_audio_from_bytes,
                        cache_image_from_bytes,
                    )

                    if msgtype == "m.image":
                        media_urls.append(
                            cache_image_from_bytes(raw, ext=".jpg" if ext == ".jpeg" else ext)
                        )
                        message_type = MessageType.IMAGE
                    elif msgtype == "m.audio":
                        media_urls.append(cache_audio_from_bytes(raw, ext=ext))
                        message_type = MessageType.AUDIO
                    media_types.append(mime)
                    text = content.get("body", "") if msgtype not in ("m.image", "m.audio") else ""
                except Exception as exc:  # noqa: BLE001
                    # Media decode is best-effort — a corrupt/undownloadable
                    # attachment must not drop the whole message. Report once,
                    # then fall through with the text we have.
                    from ..error_log import dump_chat4000_trace

                    logger.warning("chat4000: inbound media decrypt failed: %s", exc)
                    dump_chat4000_trace("matrix.inbound_media", exc)

        # build_source / handle_message come from BasePlatformAdapter, mixed in at
        # register() time (see adapter._make_adapter_class) — invisible to mypy here.
        source = self.build_source(chat_id=room_id, user_id=sender, chat_type="dm")  # type: ignore[attr-defined]
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
        await self._begin_turn(room_id)  # native typing on for the life of this turn
        try:
            await self.handle_message(event)  # type: ignore[attr-defined]
        finally:
            self._active_room = None
            # Safety net: guarantee the turn closes with idle even if no on_final
            # fired (e.g. the agent errored) and regardless of Hermes' teardown.
            await self._end_turn(room_id)

    # ─── outbound: oneshot send + typing ──────────────────────────────────

    async def send(
        self,
        chat_id: str,
        content: Any,  # noqa: ANN401  # Hermes outbound content (str or host dict)
        *,
        reply_to: Any = None,  # noqa: ANN401  # Hermes host reply ref
        metadata: Any = None,  # noqa: ANN401  # Hermes host metadata
    ) -> Any:  # noqa: ANN401  # returns Hermes host SendResult (untyped host type)
        from gateway.platforms.base import SendResult

        if self._session is None:
            return SendResult(success=False, error="not connected")
        text = content if isinstance(content, str) else (content or {}).get("text", "")
        if not text:
            return SendResult(success=True, message_id="")
        tw = self._tw(chat_id)
        anchor = await tw.start_turn(chat_id)
        if anchor:
            await tw.stream_edit(chat_id, anchor, str(text), final=True)
        await self._end_turn(chat_id)
        return SendResult(success=True, message_id=anchor or "")

    # ─── live activity (native m.typing — protocol 67919b9) ───────────────
    #
    # chat4000.status is GONE. Liveness is a single native typing on/off for the
    # whole active turn (no sub-state). Typing goes on when a turn begins, is
    # refreshed before the homeserver's 30s timeout while active, and is cleared
    # when the turn ends (final edit / error / stop_typing). _typing_on gates
    # Hermes' ~2s keep-typing loop so it can't re-light a finished room.

    async def _typing(self, room_id: str, on: bool) -> None:
        """Drive native m.typing for a room. ON re-PUTs at most every
        TYPING_REFRESH_S (well inside the 30s timeout); OFF clears it once."""
        if self._session is None:
            return
        if on:
            now = self._loop.time() if self._loop else 0.0
            last = self._typing_at.get(room_id)
            if last is not None and (now - last) < TYPING_REFRESH_S:
                return  # still fresh — no need to re-PUT
            self._typing_at[room_id] = now
            await self._tw(room_id).set_typing(room_id, typing=True, timeout_ms=TYPING_TIMEOUT_MS)
        elif room_id in self._typing_at:
            self._typing_at.pop(room_id, None)
            await self._tw(room_id).set_typing(room_id, typing=False)

    async def _begin_turn(self, room_id: str) -> None:
        """Open a turn: allow typing refreshes and signal typing on."""
        self._typing_on.add(room_id)
        await self._typing(room_id, True)

    async def _end_turn(self, room_id: str) -> None:
        """Close a turn: stop allowing typing for this room, then clear it once."""
        self._typing_on.discard(room_id)
        await self._typing(room_id, False)

    async def send_typing(
        self,
        chat_id: str,
        metadata: Any = None,  # noqa: ANN401  # Hermes host metadata
    ) -> None:
        # Hermes' keep-typing loop refreshes liveness — but only while THIS room's
        # turn is active, so it can't re-light typing after the turn ended.
        if self._session is not None and chat_id in self._typing_on:
            await self._typing(chat_id, True)

    async def stop_typing(self, chat_id: str) -> None:
        """Hermes calls this at turn teardown to clear the indicator. The base
        class is a no-op (one-shot platforms), so we override it — the canonical
        'turn done' signal that clears native typing."""
        await self._end_turn(chat_id)

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": f"chat4000 ({chat_id[:8]}…)", "type": "dm", "chat_id": chat_id}

    # ─── tool events (called from plugin_hooks) ───────────────────────────

    async def external_tool_start(
        self,
        name: str,
        args: Any,  # noqa: ANN401  # dynamic tool-call args
        icon: str = "",
    ) -> str:
        """A tool began (from pre_tool_call). Emit a chat4000.tool event related
        to the active turn; returns a tool_id to correlate the end."""
        room = self._active_room
        if not room or self._session is None:
            return ""
        anchor = await self._ensure_anchor(room)
        if anchor is None:
            return ""
        tool_id = uuid.uuid4().hex
        args_str = args if isinstance(args, str) else _json(args)
        ev = await self._tw(room).tool_start(
            room, anchor, tool_id=tool_id, name=name, args=args_str, icon=icon
        )
        self._tools[tool_id] = (room, {"name": name, "args": args_str, "event_id": ev})
        await self._typing(room, True)
        return tool_id

    async def external_tool_end(
        self, tool_id: str, *, status: str = "done", result: str = ""
    ) -> None:
        """A tool finished (from post_tool_call). Edit its event to done/failed."""
        entry = self._tools.pop(tool_id, None)
        if entry is None or self._session is None:
            return
        room, meta = entry
        if not meta.get("event_id"):
            return
        await self._tw(room).tool_end(
            room,
            meta["event_id"],
            tool_id=tool_id,
            name=meta["name"],
            args=meta["args"],
            status=status,
            result=result,
            duration_ms=0,
        )

    # ─── streaming reply pipeline (text + status; Hermes-contract) ────────

    def reply_pipeline_options(self) -> dict[str, Any]:
        if self._session is None:
            return {}

        async def on_reasoning_stream(_p: dict[str, Any]) -> None:
            if self._active_room:
                await self._typing(self._active_room, True)

        async def on_assistant_message_start(_p: dict[str, Any]) -> None:
            if self._active_room:
                await self._ensure_anchor(self._active_room)
                await self._typing(self._active_room, True)

        async def on_partial_reply(payload: dict[str, Any]) -> None:
            room = self._active_room
            text = (payload or {}).get("text") or ""
            if not room or not text:
                return
            anchor = await self._ensure_anchor(room)
            if anchor is None:
                return
            st = self._turn_state(room)
            st.last_text = text
            now = self._loop.time() if self._loop else 0.0
            if now - st.last_edit_at >= STREAM_MIN_INTERVAL_S:
                st.last_edit_at = now
                await self._tw(room).stream_edit(room, anchor, text, final=False)

        async def on_final(payload: dict[str, Any]) -> None:
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
            await self._end_turn(room)

        return {
            "on_reasoning_stream": on_reasoning_stream,
            "on_assistant_message_start": on_assistant_message_start,
            "on_partial_reply": on_partial_reply,
            "on_final": on_final,
        }

    # ─── helpers ──────────────────────────────────────────────────────────

    def _tw(self, room_id: str) -> TurnWriter:
        if self._session is None:
            raise RuntimeError("_tw called before connect() built the session")
        return self._session.turn_writer(room_id)

    def _turn_state(self, room_id: str) -> _TurnState:
        st = self._turns.get(room_id)
        if st is None:
            st = _TurnState(room_id=room_id)
            self._turns[room_id] = st
        return st

    async def _ensure_anchor(self, room_id: str) -> str | None:
        st = self._turn_state(room_id)
        if st.anchor_id is None:
            st.anchor_id = await self._tw(room_id).start_turn(room_id)
        return st.anchor_id


def _json(v: Any) -> str:  # noqa: ANN401  # dynamic tool-call args payload
    try:
        return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(v)
