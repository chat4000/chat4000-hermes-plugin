"""v2 Hermes adapter — wires MatrixSession into Hermes' BasePlatformAdapter.

Replaces the v1 relay/group-key internals with a `MatrixSession` (gateway +
OlmMachine binding + rooms). Hermes integration scaffolding (dynamic base class,
build_source/MessageEvent/handle_message) is unchanged from v1.

  inbound user message (session room) → handle_message → Hermes agent
  inbound chat4000.command (control)   → CommandHandler
  agent reply (streaming)              → TurnWriter (anchor + m.replace edits)
  agent live activity                  → chat4000.status (encrypted, refs question)
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

# Live activity = chat4000.status (encrypted timeline event, protocol e3d9358).
# Re-send the current state every STATUS_KEEPALIVE_S as a keep-alive; it must stay
# under the client's 10s TTL (a dropped event must not expire the label mid-turn).
STATUS_KEEPALIVE_S = 4.0

# How often the running gateway re-reads known-users to invite users who paired
# after it came up (the gateway-first flow — no restart needed).
INVITE_WATCH_INTERVAL_S = 3.0


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
        # Live activity (chat4000.status). Per active room: the QUESTION event_id
        # the status references, the current state, and the 4s keep-alive task.
        self._question_id: dict[str, str] = {}
        self._status_state: dict[str, str] = {}
        self._status_task: dict[str, asyncio.Task[None]] = {}
        # Users already invited this connection + the background watcher that
        # live-invites anyone who pairs AFTER the gateway is up (no restart).
        self._invited: set[str] = set()
        self._invite_task: asyncio.Task[None] | None = None

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
            # Gateway-first: self-onboard the bot identity at startup so we can
            # connect + bootstrap rooms BEFORE anyone pairs. Users attach later.
            from ..onboarding import ensure_onboarded

            try:
                creds = await ensure_onboarded(self._account_id)
            except Exception as exc:  # noqa: BLE001
                from ..error_log import dump_chat4000_trace

                logger.error("chat4000 v2: self-onboard failed: %s", exc)
                analytics.track("gateway_connect_failed", {"reason": "onboard_failed"})
                dump_chat4000_trace("matrix.onboard", exc)
                return False
            if creds is None:
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
            self._invited = set(users)
        except Exception as exc:  # noqa: BLE001
            # connect() boundary: report the unexpected failure once, then convert
            # to a False return so Hermes can surface "not connected" cleanly.
            from ..error_log import dump_chat4000_trace

            logger.error("chat4000 v2 connect failed: %s", exc)
            analytics.track("gateway_connect_failed", {"reason": type(exc).__name__})
            dump_chat4000_trace("matrix.connect", exc)
            return False

        self._connected = True
        # Wait for the first sync so 'ready' means actually-receiving — not just
        # rooms-bootstrapped (which is ~instant when the rooms already exist, so the
        # wizard's loading bar would just flash). Bounded so a stalled sync can't
        # wedge the wizard forever; we proceed either way.
        await self._session.wait_first_sync(timeout=30.0)
        self._mark_ready()  # 'gateway fully up' signal for the install wizard
        self._start_invite_watch()  # invite users who pair AFTER this point
        analytics.track("gateway_started", {"transport": "matrix"})
        return True

    def _mark_ready(self) -> None:
        from ..key_store import resolve_chat4000_ready_marker

        try:
            marker = resolve_chat4000_ready_marker()
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("ready\n", encoding="utf-8")
        except OSError as exc:
            logger.debug("could not write ready marker: %s", exc)

    def _clear_ready(self) -> None:
        import contextlib

        from ..key_store import resolve_chat4000_ready_marker

        with contextlib.suppress(OSError):
            resolve_chat4000_ready_marker().unlink(missing_ok=True)

    def _start_invite_watch(self) -> None:
        if self._loop is None or self._invite_task is not None:
            return
        self._invite_task = self._loop.create_task(self._watch_known_users())

    async def _watch_known_users(self) -> None:
        """Invite users who pair AFTER the gateway is up — no restart needed. The
        pair flow appends to known-users; we poll it and invite the newcomers."""
        from .users_store import load_known_users

        while self._connected and self._session is not None:
            try:
                users = load_known_users(self._account_id)
                new = [u for u in users if u not in self._invited]
                if new:
                    await self._session.set_members(users)
                    for uid in new:
                        await self._session.invite_user(uid)
                        self._invited.add(uid)
                        logger.info("chat4000: live-invited newly-paired user %s", uid)
            except Exception as exc:  # noqa: BLE001
                from ..error_log import dump_chat4000_trace

                dump_chat4000_trace("matrix.invite_watch", exc)
            await asyncio.sleep(INVITE_WATCH_INTERVAL_S)

    async def disconnect(self) -> None:
        self._connected = False
        if self._invite_task is not None:
            self._invite_task.cancel()
            self._invite_task = None
        self._clear_ready()
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

    async def _on_user_message(
        self, room_id: str, sender: str, content: dict[str, Any], event_id: str = ""
    ) -> None:
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
        # chat4000.status references the QUESTION (this inbound event). Open the
        # turn with "thinking" so the label shows even before the model streams.
        if event_id:
            self._question_id[room_id] = event_id
            await self._status(room_id, "thinking")
        # handle_message RETURNS IMMEDIATELY — it spawns the agent as a background
        # task (Hermes does this for interruption support). The turn's
        # working/typing transitions, the 4s keep-alive, and the final idle are
        # driven by the reply pipeline (on_final) and Hermes' turn-end hook
        # (stop_typing), which fire when the agent ACTUALLY finishes. Do NOT end the
        # status or clear _active_room here, or idle fires ~150ms in (before the
        # turn starts) and every later tool/status callback no-ops.
        try:
            await self.handle_message(event)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            # Dispatch itself failed → no background turn will run; clear the label
            # now and report. (A normal return means the turn is underway.)
            from ..error_log import dump_chat4000_trace

            dump_chat4000_trace("matrix.dispatch", exc)
            await self._end_status(room_id)

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
        await self._end_status(chat_id)
        return SendResult(success=True, message_id=anchor or "")

    # ─── live activity (chat4000.status — protocol e3d9358) ────────────────
    #
    # A fresh E2EE chat4000.status timeline event per transition, referencing the
    # QUESTION (the user's prompt event_id). Re-sent every 4s as a keep-alive while
    # the turn is active (no edit/overwrite); idle exactly once at turn end. The
    # client applies a 10s TTL, so a crashed plugin can't leave a stuck label.

    async def _status(self, room_id: str, state: str) -> None:
        """Send the current activity state and (re)start the keep-alive."""
        qid = self._question_id.get(room_id)
        if self._session is None or not qid:
            return
        self._status_state[room_id] = state
        logger.debug("chat4000.status -> %s (room=%s q=%s)", state, room_id, qid)
        await self._tw(room_id).send_status(room_id, state, qid)
        self._ensure_status_keepalive(room_id)

    def _ensure_status_keepalive(self, room_id: str) -> None:
        task = self._status_task.get(room_id)
        if task is not None and not task.done():
            return
        if self._loop is None:
            return
        self._status_task[room_id] = self._loop.create_task(self._status_keepalive(room_id))

    async def _status_keepalive(self, room_id: str) -> None:
        """Re-send the current state every STATUS_KEEPALIVE_S until the turn ends."""
        try:
            while True:
                await asyncio.sleep(STATUS_KEEPALIVE_S)
                state = self._status_state.get(room_id)
                qid = self._question_id.get(room_id)
                if self._session is None or not qid or not state or state == "idle":
                    return
                logger.debug("chat4000.status keep-alive %s (room=%s)", state, room_id)
                await self._tw(room_id).send_status(room_id, state, qid)
        except asyncio.CancelledError:
            return

    async def _end_status(self, room_id: str) -> None:
        """Close the turn: flush any still-open tool bubbles (backstop for the last
        tool), stop the keep-alive, and send idle once (success/error/abort)."""
        await self.flush_open_tools(room_id)
        task = self._status_task.pop(room_id, None)
        if task is not None:
            task.cancel()
        qid = self._question_id.get(room_id)
        if self._session is not None and qid and self._status_state.get(room_id) != "idle":
            logger.debug("chat4000.status -> idle (room=%s q=%s)", room_id, qid)
            await self._tw(room_id).send_status(room_id, "idle", qid)
        self._status_state.pop(room_id, None)
        self._question_id.pop(room_id, None)
        if self._active_room == room_id:
            self._active_room = None

    async def send_typing(
        self,
        chat_id: str,
        metadata: Any = None,  # noqa: ANN401  # Hermes host metadata
    ) -> None:
        # Native typing is removed (protocol e3d9358). Live activity is
        # chat4000.status, driven by the reply pipeline + our own 4s keep-alive —
        # NOT Hermes' typing loop. No-op so the host's keepalive does nothing here.
        return None

    async def stop_typing(self, chat_id: str) -> None:
        """Hermes' turn-end hook — clear the activity label (chat4000.status idle)."""
        await self._end_status(chat_id)

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
        started_at = self._loop.time() if self._loop else 0.0
        self._tools[tool_id] = (
            room,
            {"name": name, "args": args_str, "event_id": ev, "started_at": started_at},
        )
        await self._status(room, "working")
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
        now = self._loop.time() if self._loop else 0.0
        duration_ms = max(0, int((now - meta.get("started_at", now)) * 1000))
        await self._tw(room).tool_end(
            room,
            meta["event_id"],
            tool_id=tool_id,
            name=meta["name"],
            args=meta["args"],
            status=status,
            result=result,
            duration_ms=duration_ms,
        )

    async def flush_open_tools(self, room_id: str) -> None:
        """Close every chat4000.tool still open for this room. Hermes fires a tool's
        START (pre_tool_call) but can skip its END (post_tool_call) on
        cancel/block/thread-no-return, leaving the client spinning forever. Called
        at a round boundary (post_llm_call — a round blocks until all its tools
        finish, so anything still open is provably orphaned) AND at turn end (the
        backstop for the last tool). Idempotent: external_tool_end pops the tool, so
        a real END no-ops and this stays invisible once Hermes is fixed."""
        for tid in [t for t, (r, _m) in self._tools.items() if r == room_id]:
            await self.external_tool_end(tid, status="done", result="")

    # ─── streaming reply pipeline (text + status; Hermes-contract) ────────

    def reply_pipeline_options(self) -> dict[str, Any]:
        if self._session is None:
            return {}

        async def on_reasoning_stream(_p: dict[str, Any]) -> None:
            if self._active_room:
                await self._status(self._active_room, "thinking")

        async def on_assistant_message_start(_p: dict[str, Any]) -> None:
            if self._active_room:
                await self._ensure_anchor(self._active_room)
                await self._status(self._active_room, "typing")

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
            await self._end_status(room)

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
