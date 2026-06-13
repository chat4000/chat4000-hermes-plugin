"""v2 Hermes adapter — wires MatrixSession into Hermes' BasePlatformAdapter.

Replaces the v1 relay/group-key internals with a `MatrixSession` (gateway +
OlmMachine binding + rooms). Hermes integration scaffolding (dynamic base class,
build_source/MessageEvent/handle_message) is unchanged from v1.

  inbound user message (session room) → handle_message → Hermes agent
  inbound chat4000.command (control)   → CommandHandler
  agent reply                          → send() → TurnWriter (anchor + final edit)
  agent live activity                  → chat4000.status (encrypted, refs question)
  agent tool calls                     → chat4000.tool events, via plugin_hooks
                                         (pre_tool_call → external_tool_start)

The host delivers the finished answer through `send(chat_id=...)` — room-explicit,
so concurrent turns never cross. Tool calls flow through `plugin_hooks` (Hermes'
standard runner fires pre_tool_call), which calls `external_tool_start` here.

⚠️ Hermes-runtime-coupled; not unit-tested offline (needs the Hermes process +
the built pyvodozemac wheel).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..package_info import read_package_version
from .commands import CommandHandler
from .creds_store import load_bot_creds
from .pair_listener import CompletionListener
from .session import MatrixSession
from .turns import TurnWriter

if TYPE_CHECKING:
    from .media import MediaClient
    from .pair_codes_store import PendingCode

logger = logging.getLogger(__name__)

# Live activity = chat4000.status (encrypted timeline event, protocol e3d9358).
# Re-send the current state every STATUS_KEEPALIVE_S as a keep-alive; it must stay
# under the client's 10s TTL (a dropped event must not expire the label mid-turn).
STATUS_KEEPALIVE_S = 4.0

# How often the running gateway re-reads known-users to invite users who paired
# after it came up (the gateway-first flow — no restart needed).
INVITE_WATCH_INTERVAL_S = 3.0


class _UnroutableToolStart(Exception):
    """A tool fired but its room couldn't be resolved from per-turn identity
    (empty contextvar + no session-map hit). Raised only to carry a stable
    fingerprint into the error sink (type+message dedup, 1/hr) so the real-world
    frequency of unroutable tool starts is measurable; never propagated. We DROP
    the chip rather than misroute it to a stale, wrong-room guess."""


@dataclass
class _PendingTurn:
    """Matrix answer anchor awaiting the one push-eligible final edit."""

    anchor_id: str
    latest_text: str
    finalized: bool = False


class Chat4000MatrixAdapter:
    """Bound at register() time to BasePlatformAdapter (see adapter._make_adapter_class)."""

    SUPPORTS_MESSAGE_EDITING = True
    MAX_MESSAGE_LENGTH = 4096

    def __init__(self, config: Any, **kwargs: Any) -> None:  # noqa: ANN401  # Hermes host config/kwargs (untyped host objects)
        from gateway.config import Platform
        from gateway.platforms.base import BasePlatformAdapter

        BasePlatformAdapter.__init__(self, config=config, platform=Platform("chat4000"))
        extra = getattr(config, "extra", {}) or {}
        self._account_id = extra.get("accountId") or extra.get("account_id") or "default"
        self._session: MatrixSession | None = None
        self._commands: CommandHandler | None = None
        # Hermes session_id → room_id. The tool hooks receive only a session_id
        # (never the room), and Hermes runs sessions CONCURRENTLY, so we map each
        # session to its room — keyed exactly as Hermes builds the session key from
        # the source — and route tool events by it. An unresolved session DROPS the
        # chip (external_tool_start); there is deliberately NO global-room fallback.
        self._room_by_session: dict[str, str] = {}
        self._session_by_room: dict[str, str] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._media: MediaClient | None = None  # built at connect
        self._connected = False
        # Live activity (chat4000.status). Per active room: the QUESTION event_id
        # the status references, the current state, and the 4s keep-alive task.
        self._question_id: dict[str, str] = {}
        self._status_state: dict[str, str] = {}
        self._status_task: dict[str, asyncio.Task[None]] = {}
        self._pending_turns: dict[str, _PendingTurn] = {}
        self._html_card_finalized_for_question: dict[str, str] = {}
        # Users already invited this connection + the background watcher that
        # live-invites anyone who pairs AFTER the gateway is up (no restart).
        self._invited: set[str] = set()
        self._invite_task: asyncio.Task[None] | None = None
        # The pairing-completion listener (protocol C.4): the gateway-resident
        # system of record for every outstanding pairing code.
        self._pair_listener: CompletionListener | None = None

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
                dump_chat4000_trace("matrix.onboard", exc)
                return False
            if creds is None:
                return False

        self._session = MatrixSession(
            creds,
            account_id=self._account_id,
            plugin_version=read_package_version(),
            on_user_message=self._on_user_message,
            on_command=self._on_command,
        )
        from ..registrar_config import build_registrar_client

        # The resident listener polls GET /codes/{code} (C.3.3) which is
        # bot-token auth (C.4) — bind the bot's durable token from creds.
        listener_registrar = build_registrar_client()
        listener_registrar.set_bot_token(creds.access_token)
        pair_listener = CompletionListener(
            account_id=self._account_id,
            registrar=listener_registrar,
            on_redeem=self._on_pair_redeem,
            on_transition=self._on_pair_transition,
        )
        self._pair_listener = pair_listener
        self._commands = CommandHandler(
            self._session, version=read_package_version(), listener=pair_listener
        )

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
                    await self._ensure_initial_session(uid)
            self._invited = set(users)
        except Exception as exc:  # noqa: BLE001
            # connect() boundary: report the unexpected failure once, then convert
            # to a False return so Hermes can surface "not connected" cleanly.
            from ..error_log import dump_chat4000_trace

            logger.error("chat4000 v2 connect failed: %s", exc)
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
        # Resume completion listening for every outstanding pairing code (C.4) —
        # including codes registered before a restart and reusable ones.
        pair_listener.start()
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
                        await self._ensure_initial_session(uid)
            except Exception as exc:  # noqa: BLE001
                from ..error_log import dump_chat4000_trace

                dump_chat4000_trace("matrix.invite_watch", exc)
            await asyncio.sleep(INVITE_WATCH_INTERVAL_S)

    async def _ensure_initial_session(self, user_id: str) -> None:
        """Auto-create ONE initial session room for a paired user + invite them, so
        their first chat works without pressing "New Session".

        DURABLE dedupe: the onboarded store records user→room, so a restart — which
        re-reads known-users and re-invites everyone — never mints a SECOND initial
        room (the per-connection `_invited` set is not durable; this store is).

        Invite-failure policy: `invite_user` only LOGS HTTP failures (never raises),
        so the room is created + marked onboarded even if the invite didn't land; the
        invite-watch loop re-invites known users to every room, self-healing a missing
        invite WITHOUT a duplicate room. One user's failure (e.g. a network throw
        before the room is made) must not break connect()/the watch loop, so we report
        and swallow here; a throw before the mark just means the next poll retries."""
        from .users_store import load_onboarded, mark_onboarded

        if self._session is None or self._session.rooms is None:
            return
        if user_id in load_onboarded(self._account_id):
            return
        try:
            room_id = await self._session.rooms.create_session_room_and_invite([user_id])
            mark_onboarded(user_id, room_id, self._account_id)
            logger.info("chat4000: auto-created initial session room %s for %s", room_id, user_id)
        except Exception as exc:  # noqa: BLE001
            from ..error_log import dump_chat4000_trace

            dump_chat4000_trace("matrix.auto_initial_session", exc, {"user_id": user_id})

    # ─── pairing completion (resident listener, protocol C.4) ─────────────

    async def _on_pair_redeem(
        self, record: PendingCode, status: dict[str, Any], entry: dict[str, Any]
    ) -> None:
        """A device redeemed an outstanding code. Membership needs nothing — the
        user's invites pre-exist from setup (C.6) and a device added to an
        already-joined user inherits membership; room KEYING for the new device
        rides the plugin's normal next-send key share (never pre-shared, C.3).
        Our jobs: record the (one) user so the invite watch self-heals a missing
        invite/initial room, and the PL4 `pairing_completed` join event — once
        per redeemed device, for EVERY code kind (late redeems of long-lived /
        reusable codes included). Dedupe against the CLI watcher is the store's
        `redeemed_count_seen` check-and-set: the listener only hands us entries
        beyond the recorded count, and the CLI advances the same field when it
        reports a redeem first. In-process `device.pair_start` watchers claim()
        their code, so this never overlaps those either."""
        from .. import analytics
        from .registrar_client import pair_redeem_index
        from .users_store import add_known_user

        user_id = str(status.get("user_id") or "")
        if user_id:
            add_known_user(user_id, self._account_id)
            # Refresh the redeeming user's device keys (protocol E, "Refresh the new
            # device's keys on redeem"): FORCE a /keys/query so the agent learns the
            # just-paired device and its next send Megolm-shares the room key to it.
            # The user is tracked from setup (with zero devices then), so a plain
            # re-track is idempotent and would NOT re-query — we must force it. This
            # is in addition to (not a replacement for) the best-effort
            # device_lists.changed sync delta, which is empty on initial sync and
            # racy. Best-effort: a failure here must not break redeem handling.
            crypto = self._session.crypto if self._session is not None else None
            if crypto is not None:
                try:
                    await crypto.force_query_user(user_id)
                except Exception as exc:  # noqa: BLE001
                    from ..error_log import dump_chat4000_trace

                    logger.warning("force key re-query for %s failed: %s", user_id, exc)
                    dump_chat4000_trace("matrix.force_query_user", exc, {"user_id": user_id})
        logger.info(
            "chat4000: pairing redeem observed (device=%s, reusable=%s)",
            entry.get("device_id"),
            record.reusable,
        )
        analytics.track_pairing_completed(
            str(entry.get("client_id") or status.get("client_id") or "").strip() or None,
            reusable=record.reusable,
            redeem_index=pair_redeem_index(status, entry.get("device_id")),
        )

    async def _on_pair_transition(
        self, record: PendingCode, state: str, status: dict[str, Any]
    ) -> None:
        """An outstanding code settled under the resident listener. Only
        `device.pair_start` codes have a control-room lifecycle to finish — emit
        the `chat4000.pair_status` event (E) the in-process watcher would have
        sent had the gateway not restarted mid-window."""
        if not record.pair_id or self._session is None:
            return
        rooms = self._session.rooms
        crypto = self._session.crypto
        control = rooms.control_room_id if rooms is not None else None
        if control is None or crypto is None:
            logger.warning("no control room; cannot send resumed pair status %s", state)
            return
        content = {
            "msgtype": "chat4000.pair_status",
            "pair_id": record.pair_id,
            "state": state,
        }
        await crypto.send_room_event(
            control, "m.room.message", content, self._session.recipients(control), push=False
        )

    async def disconnect(self) -> None:
        self._connected = False
        if self._invite_task is not None:
            self._invite_task.cancel()
            self._invite_task = None
        if self._pair_listener is not None:
            await self._pair_listener.stop()
            self._pair_listener = None
        self._clear_ready()
        from ..plugin_hooks import deregister_active_adapter

        deregister_active_adapter(self)
        if self._session is not None:
            await self._session.stop()
            self._session = None
        from .. import analytics

        # DEC3: no gateway_stopped event — just flush so any pending
        # pairing_completed from the resident listener lands before exit.
        analytics.flush()

    # ─── inbound callbacks (from MatrixSession) ───────────────────────────

    async def _on_command(
        self, room_id: str, command: str, content: dict[str, Any], sender: str
    ) -> None:
        if self._commands is not None:
            await self._commands.handle(room_id, command, content, sender=sender)

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

        # chat4000.status references the QUESTION (this inbound event). Open the turn
        # with "thinking" BEFORE the media fetch below — a voice note's download +
        # decrypt can take several seconds, and firing the label first means the user
        # sees immediate feedback instead of a silent gap during that fetch.
        if event_id:
            self._html_card_finalized_for_question.pop(room_id, None)
            self._question_id[room_id] = event_id
            await self._status(room_id, "thinking")

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
                        message_type = MessageType.PHOTO  # host enum has PHOTO, not IMAGE
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
        if text:
            await self._maybe_set_first_message_title(room_id, text)
        # Remember which room this Hermes session belongs to, so the tool hooks
        # (which only know the session_id) route their bubbles to THIS room even
        # while another session runs concurrently.
        self._remember_session_room(source, room_id)
        event = MessageEvent(
            text=text,
            message_type=message_type,
            source=source,
            raw_message=content,
            message_id=uuid.uuid4().hex,
            media_urls=media_urls,
            media_types=media_types,
        )
        # handle_message RETURNS IMMEDIATELY — it spawns the agent as a background
        # task (Hermes does this for interruption support). After "thinking", the
        # live transitions are "working" (each tool start) and the final "idle"
        # (Hermes' turn-end hook stop_typing, and send()). Do NOT end the status
        # here, or idle fires ~150ms in (before the turn starts) and every later
        # tool/status callback no-ops.
        try:
            await self._handle_message_with_title_callback(event, room_id)
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
        text = self._content_text(content)
        if not text:
            return SendResult(success=True, message_id="")
        if self._html_card_finalized(room_id=chat_id):
            return SendResult(success=True, message_id="")
        tw = self._tw(chat_id)
        anchor = await tw.start_turn(chat_id)
        if anchor:
            final = not self._turn_is_active(chat_id)
            await tw.stream_edit(chat_id, anchor, text, final=final)
            if not final:
                self._pending_turns[chat_id] = _PendingTurn(anchor_id=anchor, latest_text=text)
        return SendResult(success=True, message_id=anchor or "")

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        finalize: bool = False,
        metadata: Any = None,  # noqa: ANN401  # accepted for Hermes stream-consumer compat
    ) -> Any:  # noqa: ANN401  # returns Hermes host SendResult (untyped host type)
        from gateway.platforms.base import SendResult

        if self._session is None:
            return SendResult(success=False, error="not connected")
        text = self._content_text(content)
        if not text:
            return SendResult(success=True, message_id=message_id)
        if self._html_card_finalized(room_id=chat_id):
            return SendResult(success=True, message_id=message_id)

        await self._tw(chat_id).stream_edit(chat_id, message_id, text, final=False)
        if self._turn_is_active(chat_id):
            pending = self._pending_turns.get(chat_id)
            if pending is None or pending.anchor_id != message_id:
                pending = _PendingTurn(anchor_id=message_id, latest_text=text)
                self._pending_turns[chat_id] = pending
            else:
                pending.latest_text = text
                pending.finalized = False
        return SendResult(success=True, message_id=message_id)

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
        """Close the turn: stop the keep-alive and send idle once (success/error/
        abort). Tool events are START-only (protocol E) — nothing to flush."""
        task = self._status_task.pop(room_id, None)
        if task is not None:
            task.cancel()
        qid = self._question_id.get(room_id)
        if self._session is not None and qid and self._status_state.get(room_id) != "idle":
            logger.debug("chat4000.status -> idle (room=%s q=%s)", room_id, qid)
            await self._tw(room_id).send_status(room_id, "idle", qid)
            self._status_state[room_id] = "idle"  # record (dedupe a second end), don't drop
        # Deliberately DO NOT clear _question_id here. A rapid / interrupting
        # follow-up message may have ALREADY claimed this room (set its own
        # question_id); clearing on the OLD turn's teardown would wipe the NEW turn's
        # context → it would run with no thinking and no tool bubbles (the answer
        # still ships via send(), which is why you'd see a reply with no activity).
        # _on_user_message overwrites _question_id with the latest turn and re-arms
        # the keep-alive, so the latest turn's status self-heals.

    async def send_typing(
        self,
        chat_id: str,
        metadata: Any = None,  # noqa: ANN401  # Hermes host metadata
    ) -> None:
        # Native typing is removed (protocol e3d9358). Live activity is
        # chat4000.status, driven by message receipt + tool starts + our own 4s
        # keep-alive — NOT Hermes' typing loop. No-op so the host's keepalive does
        # nothing here.
        return None

    async def stop_typing(self, chat_id: str) -> None:
        """Hermes' turn-end hook: finalize the answer push, then clear activity."""
        finalize_error: Exception | None = None
        try:
            await self._finalize_pending_turn(chat_id)
        except Exception as exc:  # noqa: BLE001
            from ..error_log import dump_chat4000_trace

            finalize_error = exc
            dump_chat4000_trace("matrix.finalize_turn", exc, {"room_id": chat_id})
        await self._end_status(chat_id)
        if finalize_error is not None:
            raise finalize_error

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": f"chat4000 ({chat_id[:8]}…)", "type": "dm", "chat_id": chat_id}

    # ─── tool events (called from plugin_hooks) ───────────────────────────

    async def external_tool_start(
        self,
        name: str,
        args: Any = None,  # noqa: ANN401  # accepted for hook compat; NOT sent (START-only)
        icon: str = "",
        session_id: str = "",
        room: str = "",
    ) -> str:
        """A tool started (from pre_tool_call). Emit ONE START-only chat4000.tool
        event into the firing TURN's room — `room` comes from Hermes' task-local chat
        contextvar (correct under concurrency); the session map is a last-resort
        per-turn fallback. If NEITHER resolves a room we DROP the chip and report it
        (no global fallback — that would misroute it under concurrency). Per protocol
        E the event carries only {tool_id, name, icon?} — no end, no edit, no
        args/result/status/duration. Also drives the 'working' live label. Returns
        the tool_id correlator (empty string when dropped)."""
        room = room or self._room_for_session(session_id) or ""
        if self._session is None:
            return ""  # not connected — benign, nothing to report
        if not room:
            # Per-turn identity gave us no room and we REFUSE the global fallback
            # (it would leak the chip into the wrong concurrent room). Drop the
            # event and report it: pending verification of the host's
            # HERMES_SESSION_CHAT_ID contract during pre_tool_call, an unresolved
            # room is UNEXPECTED. The sink dedups by type+message at 1/hr, so a
            # high-frequency empty case collapses to one counted entry.
            from ..error_log import dump_chat4000_trace

            dump_chat4000_trace(
                "matrix.tool_start_unroutable",
                _UnroutableToolStart(f"no room for tool {name!r}"),
                {"tool_name": name, "session_id": session_id},
            )
            return ""
        tool_id = uuid.uuid4().hex
        ev = await self._tw(room).tool_start(room, tool_id=tool_id, name=name, icon=icon)
        logger.debug(
            "tool start: id=%s name=%s room=%s session=%s event=%s",
            tool_id,
            name,
            room,
            session_id,
            ev,
        )
        await self._status(room, "working")
        return tool_id

    async def external_html_card(self, html: str, *, session_id: str = "", room: str = "") -> str:
        """Send the Chat4000-specific HTML-card final-answer event.

        Called only by the internal `final_card` plugin tool after it
        has verified the Hermes session context is a Chat4000 gateway turn. The
        card event itself is the visible result, so this method marks the room's
        current turn as card-finalized; later text sends/edits and the turn-end
        finalizer become no-ops for that same question."""
        room = room or self._room_for_session(session_id) or ""
        if self._session is None or not room or not html:
            return ""

        event_id = await self._tw(room).html_card(room, html=html)
        if not event_id:
            return ""

        self._html_card_finalized_for_question[room] = self._question_id.get(room, "")
        logger.debug(
            "html card final answer: room=%s session=%s event=%s",
            room,
            session_id,
            event_id,
        )
        return event_id

    # ─── helpers ──────────────────────────────────────────────────────────

    def _remember_session_room(self, source: Any, room_id: str) -> None:  # noqa: ANN401  # Hermes SessionSource
        """Record the Hermes session→room mapping for this turn. The key is built
        with Hermes' OWN build_session_key, so it matches exactly the session_id the
        tool hooks later receive (Hermes builds it deterministically from the same
        source). Best-effort: on failure the tool hook can't resolve the room and
        drops the chip (there is no global-room fallback)."""
        try:
            from gateway.session import build_session_key

            extra = getattr(getattr(self, "config", None), "extra", {}) or {}
            skey = build_session_key(
                source,
                group_sessions_per_user=extra.get("group_sessions_per_user", True),
                thread_sessions_per_user=extra.get("thread_sessions_per_user", False),
            )
            self._room_by_session[skey] = room_id
            self._session_by_room[room_id] = skey
        except Exception as exc:  # noqa: BLE001
            from ..error_log import dump_chat4000_trace

            dump_chat4000_trace("matrix.session_room_map", exc)

    async def _maybe_set_first_message_title(self, room_id: str, text: str) -> None:
        if self._session is None or self._session.rooms is None:
            return
        await self._session.rooms.maybe_set_first_message_title(room_id, text)

    async def _apply_host_session_title(self, room_id: str, title: str) -> None:
        if self._session is None or self._session.rooms is None:
            return
        await self._session.rooms.maybe_apply_host_title(room_id, title)

    async def _finalize_pending_turn(self, room_id: str) -> None:
        pending = self._pending_turns.get(room_id)
        if pending is None:
            return
        if self._html_card_finalized(room_id=room_id):
            self._pending_turns.pop(room_id, None)
            return
        if pending.finalized:
            self._pending_turns.pop(room_id, None)
            return
        if pending.latest_text:
            await self._tw(room_id).stream_edit(
                room_id, pending.anchor_id, pending.latest_text, final=True
            )
        pending.finalized = True
        self._pending_turns.pop(room_id, None)

    def _turn_is_active(self, room_id: str) -> bool:
        if not self._question_id.get(room_id):
            return False
        return self._status_state.get(room_id) != "idle"

    @staticmethod
    def _content_text(content: Any) -> str:  # noqa: ANN401  # Hermes content can be str/dict/etc.
        if isinstance(content, str):
            return content
        if isinstance(content, dict):
            return str(content.get("text") or "")
        if content is None:
            return ""
        return str(content)

    def _html_card_finalized(self, *, room_id: str) -> bool:
        marker = self._html_card_finalized_for_question.get(room_id)
        if marker is None:
            return False
        return marker == self._question_id.get(room_id, "")

    def _schedule_host_session_title(self, room_id: str, title: str) -> None:
        if self._loop is None or not self._loop.is_running():
            return
        coro = self._apply_host_session_title(room_id, title)
        try:
            on_loop_thread = asyncio.get_running_loop() is self._loop
        except RuntimeError:
            on_loop_thread = False
        try:
            if on_loop_thread:
                self._loop.create_task(coro)
            else:
                asyncio.run_coroutine_threadsafe(coro, self._loop)
        except RuntimeError as exc:
            logger.debug("could not schedule host session title: %s", exc)
            coro.close()

    async def _handle_message_with_title_callback(self, event: Any, room_id: str) -> None:  # noqa: ANN401
        handle = self.handle_message  # type: ignore[attr-defined]
        if not self._accepts_title_callback(handle):
            await handle(event)
            return

        def title_callback(title: str) -> None:
            self._schedule_host_session_title(room_id, title)

        await handle(event, title_callback=title_callback)

    @staticmethod
    def _accepts_title_callback(handle: Any) -> bool:  # noqa: ANN401
        try:
            sig = inspect.signature(handle)
        except (TypeError, ValueError):
            return False
        for param in sig.parameters.values():
            if param.kind is inspect.Parameter.VAR_KEYWORD:
                return True
            if param.name == "title_callback":
                return True
        return False

    def _room_for_session(self, session_id: str) -> str | None:
        """Resolve which room a tool hook belongs to from its session_id, by
        PER-TURN identity only. Exact map hit first; then recover the room embedded
        in the session key (Hermes keys embed chat_id == room_id) in case the id
        carries extra decoration. Returns None when it can't be resolved — there is
        deliberately NO global-room fallback: any single "current room" is
        last-written by the most recent inbound message, so under concurrent turns it
        points at the WRONG room and would render a tool chip in another user's room
        (a cross-room leak). An unresolved room must drop the chip, not misroute it."""
        if not session_id:
            return None
        room = self._room_by_session.get(session_id)
        if room:
            return room
        for known in self._room_by_session.values():
            if known and (session_id.endswith(known) or f":{known}:" in session_id):
                return known
        return None

    def _tw(self, room_id: str) -> TurnWriter:
        if self._session is None:
            raise RuntimeError("_tw called before connect() built the session")
        return self._session.turn_writer(room_id)
