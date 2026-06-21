"""MatrixSession — the v2 orchestrator.

Owns the live Matrix session for one plugin bot identity: builds the OlmMachine
binding + gateway + crypto driver + room manager, runs the sync loop, decrypts
inbound timeline events, and routes them:

  - `chat4000.command` in the CONTROL room   → on_command  (session.* / plugin.*)
  - `chat4000.command` anywhere else          → IGNORED (the command boundary, E)
  - `m.room.message` in a session room        → on_user_message → Hermes
  - our own events                            → ignored

Outbound replies use `turn_writer(room_id)`. The adapter supplies the two async
callbacks and drives replies; this class knows nothing about Hermes.

Dependencies are built in `start()` but can be injected for tests.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from .creds_store import BotCreds, crypto_store_path
from .crypto_driver import CryptoDriver, OlmMachineLike, load_olm_machine
from .cursor_store import CursorStore
from .gateway_client import GatewayClient, GatewayCredentials
from .rooms import RoomManager
from .sliding_sync import build_sync_request
from .turns import TurnWriter

logger = logging.getLogger(__name__)

# Cap on remembered control-command event_ids (FIFO eviction). Bounds memory while
# covering any realistic re-delivery window.
_MAX_HANDLED_COMMAND_EVENTS = 512

# (room_id, sender, content, event_id) — event_id is the QUESTION event (for status)
UserMessageCb = Callable[[str, str, dict[str, Any], str], Awaitable[None]]
CommandCb = Callable[
    [str, str, dict[str, Any], str], Awaitable[None]
]  # (room_id, command, content, sender)

APP_ID = "@chat4000/hermes-plugin"


async def _noop_msg(room_id: str, sender: str, content: dict[str, Any], event_id: str) -> None: ...
async def _noop_cmd(room_id: str, command: str, content: dict[str, Any], sender: str) -> None: ...


class MatrixSession:
    def __init__(
        self,
        creds: BotCreds,
        *,
        account_id: str = "default",
        plugin_version: str = "0.0.0",
        on_user_message: UserMessageCb = _noop_msg,
        on_command: CommandCb = _noop_cmd,
    ) -> None:
        self._creds = creds
        self._account_id = account_id
        self._plugin_version = plugin_version
        self._on_user_message = on_user_message
        self._on_command = on_command

        self.gateway: GatewayClient | None = None
        self.crypto: CryptoDriver | None = None
        self.rooms: RoomManager | None = None
        self._machine: OlmMachineLike | None = None
        self._members: list[str] = []
        # room_id → set of currently-joined MXIDs (the real key-share recipients),
        # learned from each sync's `m.room.member` state. Excludes the bot.
        self._room_members: dict[str, set[str]] = {}
        # The set we've asked the OlmMachine to track device lists for (so we only
        # re-issue /keys/query when it actually changes).
        self._tracked: set[str] = set()
        # event_ids of control commands we've already dispatched. A control event
        # can be RE-DELIVERED (a fresh re-sync after sync_reset/reconnect replays
        # recent timeline); without this, each replay re-runs the command — e.g.
        # device.pair_start minting a NEW pairing code every time (the "too many
        # active codes" storm). Insertion-ordered dict used as a bounded FIFO.
        self._handled_command_events: dict[str, None] = {}
        # Set after the FIRST sync frame is processed — the gateway is then truly
        # receiving + crypto-warm. The 'ready' signal waits on this, not just on
        # room bootstrap (which completes ~instantly when rooms already exist).
        self._first_sync = asyncio.Event()

    # ─── lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Build the stack and go live. Raises if the pyvodozemac binding isn't
        installed (the wheel must be built — see ../chat4000-pyvodozemac/BUILD.md)."""
        try:
            self._machine = load_olm_machine(
                self._creds.user_id, self._creds.device_id, crypto_store_path(self._account_id)
            )
        except ImportError as exc:
            raise RuntimeError(
                "chat4000_pyvodozemac binding not installed — build the maturin "
                "wheel (see ../chat4000-pyvodozemac/BUILD.md) before connecting"
            ) from exc

        self.gateway = GatewayClient(
            GatewayCredentials(
                gateway_url=self._creds.gateway_url,
                access_token=self._creds.access_token,
                app_id=APP_ID,
                client_version=self._plugin_version,
                platform="linux",
            ),
            on_sync=self._on_sync,
            # Durably persist + replay BOTH sync cursors (protocol D) so a process
            # restart resumes an incremental sync and keeps the device_lists delta.
            cursor_store=CursorStore(self._account_id),
        )
        self.crypto = CryptoDriver(self._machine, self.gateway)
        self.rooms = RoomManager(self.gateway, self._creds.server_name)

        self.gateway.start()
        await self.gateway.wait_authed()
        await self.gateway.start_sync(build_sync_request())

    async def ensure_bootstrap(self) -> None:
        """Create the space + control room if we don't have them yet. Idempotent:
        once the first sync classifies an existing control room, this no-ops."""
        if self.rooms is None:
            raise RuntimeError("ensure_bootstrap called before start() built the room manager")
        # Find existing space/control room first so a restart doesn't duplicate them.
        await self.rooms.discover()
        if self.rooms.space_id is None:
            await self.rooms.create_space()
        if self.rooms.control_room_id is None:
            await self.rooms.create_control_room()

    async def stop(self) -> None:
        if self.gateway is not None:
            await self.gateway.close()

    # ─── members / key sharing ────────────────────────────────────────────

    @property
    def members(self) -> list[str]:
        return list(self._members)

    @property
    def access_token(self) -> str:
        """The bot's durable access token — the proof for the registrar's
        bot-token endpoints (PUT /user, POST /codes, GET /codes — C.4)."""
        return self._creds.access_token

    async def invite_user(self, user_id: str) -> None:
        """Invite a paired user into the space + control room (their entry point;
        session rooms are invited as they're created)."""
        if self.rooms is None:
            return
        for r in (self.rooms.space_id, self.rooms.control_room_id):
            if r:
                await self.rooms.invite_user(r, user_id)

    async def set_members(self, user_ids: list[str]) -> None:
        """The paired user(s) — the floor of the key-share recipient set. Actual
        per-room recipients are this UNION the room's live joined membership (so a
        user who joined after connect still gets keys). Triggers device tracking."""
        self._members = list(user_ids)
        await self._maybe_track()

    @property
    def _bot_id(self) -> str | None:
        return self.gateway.user_id if self.gateway is not None else None

    def recipients(self, room_id: str) -> list[str]:
        """Who to share this room's Megolm key with: the paired members UNION the
        room's currently-joined membership, minus the bot itself."""
        s = set(self._members) | self._room_members.get(room_id, set())
        s.discard(self._bot_id)
        s.discard(None)
        return sorted(s)

    async def _maybe_track(self) -> None:
        """Recompute the union of everyone we must share keys with (paired members
        + every room's joined members) and, if it changed, tell the OlmMachine to
        track their device lists (issues /keys/query). Idempotent."""
        want = set(self._members)
        for joined in self._room_members.values():
            want |= joined
        want.discard(self._bot_id)
        want.discard(None)
        if want != self._tracked and self.crypto is not None:
            self._tracked = set(want)
            await self.crypto.track_users(sorted(want))

    async def _update_room_membership(self, room_id: str, room: dict[str, Any]) -> None:
        """Apply one room's `m.room.member` changes to our recipient set, then
        re-track if the union grew. Joins add, leave/ban/invite-only do not count
        as recipients (only joined devices can use the key)."""
        from .sliding_sync import extract_membership

        memberships = extract_membership(room)
        if not memberships:
            return
        bot = self._bot_id
        cur = self._room_members.setdefault(room_id, set())
        for mxid, membership in memberships.items():
            if mxid == bot:
                continue
            if membership == "join":
                cur.add(mxid)
            else:
                cur.discard(mxid)
        await self._maybe_track()

    def turn_writer(self, room_id: str) -> TurnWriter:
        if self.crypto is None or self.gateway is None:
            raise RuntimeError("turn_writer called before start() built the crypto/gateway stack")
        return TurnWriter(self.crypto, self.gateway, self.recipients(room_id))

    # ─── sync loop + routing ──────────────────────────────────────────────

    async def wait_first_sync(self, timeout: float = 30.0) -> bool:
        """Block until the first sync frame has been processed (the gateway is then
        truly up + receiving). Returns False on timeout (caller proceeds anyway)."""
        try:
            await asyncio.wait_for(self._first_sync.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False

    async def _on_sync(self, frame: dict[str, Any]) -> None:
        if self.crypto is None or self.rooms is None:
            raise RuntimeError("_on_sync fired before start() built the crypto/room stack")
        self._first_sync.set()  # gateway is receiving syncs → fully up
        parsed = await self.crypto.process_sync(frame)
        for room_id, r in parsed.rooms.items():
            self.rooms.classify_room(room_id, r.get("required_state", []))
            # Learn who's actually in the room → drives key-share recipients +
            # device tracking. MUST run before we reply into the room.
            await self._update_room_membership(room_id, r)
            for ev in r.get("timeline", []):
                if ev.get("type") == "m.room.encrypted":
                    await self._handle_encrypted(room_id, ev)

    async def _handle_encrypted(self, room_id: str, ev: dict[str, Any]) -> None:
        sender = ev.get("sender")
        if self.gateway is not None and sender == self.gateway.user_id:
            return  # ignore our own echoes
        if self.crypto is None:
            raise RuntimeError("_handle_encrypted fired before start() built the crypto stack")
        logger.debug(
            "incoming encrypted event room=%s event_id=%s sender=%s",
            room_id,
            ev.get("event_id"),
            sender,
        )
        clear = await self.crypto.decrypt(ev, room_id)
        if clear is None:
            # The room key likely just hasn't arrived yet (separate to-device
            # channel) — we currently DROP it with no retry (the inbound key-race
            # bug). Log what was lost so the gap is visible until buffer-retry lands.
            logger.debug(
                "DROP undecryptable event room=%s event_id=%s session=%s sender=%s "
                "(key not arrived; no retry)",
                room_id,
                ev.get("event_id"),
                (ev.get("content") or {}).get("session_id"),
                sender,
            )
            return
        content = clear.get("content") or {}
        msgtype = content.get("msgtype")
        logger.debug(
            "decrypted room=%s event_id=%s type=%s msgtype=%s",
            room_id,
            ev.get("event_id"),
            clear.get("type"),
            msgtype,
        )

        # Command boundary (E): only honor chat4000.command in the control room.
        if msgtype == "chat4000.command":
            if self.rooms is not None and room_id == self.rooms.control_room_id:
                # Dedup by event_id: a re-delivered command (a fresh re-sync after
                # sync_reset/reconnect replays recent timeline) MUST NOT re-run, or
                # device.pair_start mints a new pairing code per replay (the "too
                # many active codes" storm). A genuine retry carries a new event_id.
                event_id = ev.get("event_id") or ""
                if event_id and event_id in self._handled_command_events:
                    logger.debug(
                        "skip duplicate command=%s event_id=%s (already handled)",
                        content.get("command"),
                        event_id,
                    )
                    return
                if event_id:
                    self._handled_command_events[event_id] = None
                    if len(self._handled_command_events) > _MAX_HANDLED_COMMAND_EVENTS:
                        self._handled_command_events.pop(
                            next(iter(self._handled_command_events))
                        )
                logger.debug(
                    "routing command=%s room=%s (control)", content.get("command"), room_id
                )
                await self._on_command(room_id, content.get("command", ""), content, sender or "")
            else:
                logger.info("ignoring chat4000.command outside control room (%s)", room_id)
            return

        # User message in a session room → mark read (so the client shows a "read"
        # tick), then hand up to Hermes. The event_id is the QUESTION the turn's
        # chat4000.status events reference.
        if clear.get("type") == "m.room.message" and sender:
            event_id = ev.get("event_id") or ""
            logger.debug(
                "routing user message room=%s sender=%s event_id=%s -> hermes",
                room_id,
                sender,
                event_id,
            )
            await self._mark_read(room_id, event_id or None)
            await self._on_user_message(room_id, sender, content, event_id)

    async def _mark_read(self, room_id: str, event_id: str | None) -> None:
        """Send a public `m.read` receipt for the user's message so their client
        can render the read tick. Best-effort — never breaks message handling.

        Note: the user's client only SEES this if its own sync subscribes to the
        receipts extension (client/gateway side); sending it is the plugin's part."""
        if not event_id or self.gateway is None:
            return
        try:
            await self.gateway.request(
                "POST",
                f"/_matrix/client/v3/rooms/{room_id}/receipt/m.read/{event_id}",
                {},
            )
        except Exception as exc:  # noqa: BLE001
            # Read receipts are a best-effort side channel — never break message
            # handling. Report once to the sink, then continue.
            from ..error_log import dump_chat4000_trace

            logger.debug("read receipt failed in %s: %s", room_id, exc)
            dump_chat4000_trace("matrix.read_receipt", exc)
