"""WS Gateway client — the single WebSocket the plugin holds (protocol D).

Speaks the gateway's muxed frame envelope (verified against the real
`chat4000-matrix-ws-proxy/src/protocol.rs`):

  client → gateway:  auth, sync_start, sync_update, sync_ack, sync_stop, req
  gateway → client:  auth_ok, auth_error, reauth, resp, error, sync, sync_reset

Responsibilities:
  - `auth` handshake (+ re-auth on `reauth` without dropping the socket)
  - `request()` — forward a C-S call as a `req` frame, await the matched `resp`
    by `id` (this is how ALL homeserver calls go out: keys/*, sendToDevice,
    createRoom, send, invite, …)
  - drive sliding sync from `sync_start`/`sync_update`, push each `sync` frame
    to a callback, and (per D.1) `sync_ack` only after the consumer has durably
    persisted the batch — the anti-UTD discipline
  - reconnect with backoff, resending the device's last durably-persisted `pos`

⚠️ DEPLOYED-GATEWAY GAP (pushback X-sync-ack): the live proxy's `protocol.rs`
has no `sync_ack` frame and auto-advances the upstream cursor. We send `sync_ack`
per the spec anyway (forward-compatible); until the gateway honors it, to-device
room keys can be deleted before the crypto store persists them → UTD. This is a
hard upstream dependency, not something the client can fix alone.

This module does NO Matrix crypto and NO room logic — it is a dumb, reliable
transport. Crypto is `crypto_driver.py` + the pyvodozemac binding; rooms/turns
live above.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

from ..error_log import dump_chat4000_trace
from ..reconnect import run_with_reconnect
from .cursor_store import CursorStore

logger = logging.getLogger(__name__)

SyncHandler = Callable[[dict[str, Any]], Awaitable[None]]
ReauthHandler = Callable[[], Awaitable[str | None]]  # returns a fresh token or None


@dataclass
class GatewayCredentials:
    gateway_url: str
    access_token: str
    # Client identity (D.1) — recorded by the gateway, drives the coarse version
    # gate. `client_version` becomes required once the gateway sets a bound.
    app_id: str
    client_version: str
    platform: str = "linux"
    release_channel: str = "production"


class AuthError(RuntimeError):
    """The gateway rejected `auth` (bad token, or unsupported client version)."""

    def __init__(self, reason: str, min_v: str | None, max_v: str | None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.min_client_version = min_v
        self.max_client_version = max_v


class GatewayClient:
    """One live socket. Construct, `await connect()`, then `request()` / sync."""

    def __init__(
        self,
        creds: GatewayCredentials,
        *,
        on_sync: SyncHandler,
        on_reauth: ReauthHandler | None = None,
        request_timeout: float = 30.0,
        abort_signal: asyncio.Event | None = None,
        cursor_store: CursorStore | None = None,
    ) -> None:
        self._creds = creds
        self._on_sync = on_sync
        self._on_reauth = on_reauth
        self._request_timeout = request_timeout
        self._abort = abort_signal or asyncio.Event()
        # Durable home for the two sliding-sync cursors (protocol D). Persisted on
        # every ack and loaded here so a PROCESS restart resumes an INCREMENTAL sync
        # (a fresh, cursor-less sync would drop the device_lists delta — a D violation).
        self._cursor_store = cursor_store

        self._ws: ClientConnection | None = None
        self._pending: dict[str, asyncio.Future[tuple[int, dict[str, Any]]]] = {}
        self._send_lock = asyncio.Lock()

        # Sliding-sync state the CLIENT owns (the gateway keeps pos only in
        # memory for the socket; on reconnect we resend our last persisted pos).
        self._sync_body: dict[str, Any] | None = None
        # Two independent upstream cursors (protocol D): the room cursor (`pos`)
        # and the to-device cursor. The to-device cursor carries the delete-on-read
        # Megolm room keys — it is NEVER derived from `pos`; we persist + ack + resume
        # it separately, and carry the last value forward on frames with none.
        self._last_persisted_pos: str | None = None
        self._last_persisted_to_device_pos: str | None = None
        # Replay durably-persisted cursors so a fresh process resumes incrementally
        # (protocol D, "Refresh the new device's keys on redeem": a restart that
        # begins a cursor-less sync silently drops the device_lists delta).
        if self._cursor_store is not None:
            persisted = self._cursor_store.load()
            self._last_persisted_pos = persisted.pos
            self._last_persisted_to_device_pos = persisted.to_device_pos

        self.user_id: str | None = None
        self.device_id: str | None = None
        self._run_task: asyncio.Task[None] | None = None
        self._authed = asyncio.Event()
        # Sync frames are handled on a SEPARATE worker, never inline in the read
        # loop — see _sync_worker_loop for why (deadlock avoidance).
        self._sync_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._sync_worker: asyncio.Task[None] | None = None

    # ─── lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the reconnecting run loop. Returns immediately; await
        `wait_authed()` to block until the first successful auth."""
        self._run_task = asyncio.ensure_future(self._run_forever())

    async def wait_authed(self, timeout: float | None = None) -> None:
        await asyncio.wait_for(
            self._authed.wait(),
            timeout=self._request_timeout if timeout is None else timeout,
        )

    async def close(self) -> None:
        self._abort.set()
        if self._ws is not None:
            with contextlib.suppress(OSError, websockets.WebSocketException):
                await self._ws.close()
        if self._run_task is not None:
            try:
                await asyncio.wait_for(self._run_task, timeout=2.0)
            except (TimeoutError, asyncio.CancelledError):
                # Run loop didn't exit in time (or we were cancelled) — force it down.
                self._run_task.cancel()

    # ─── public API used by the crypto driver / room layer ────────────────

    async def request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> tuple[int, dict[str, Any]]:
        """Forward a C-S API call. Returns `(status, body)`. This is THE way
        every homeserver call leaves the plugin."""
        await self.wait_authed()
        rid = uuid.uuid4().hex[:32]
        fut: asyncio.Future[tuple[int, dict[str, Any]]] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        frame: dict[str, Any] = {"t": "req", "id": rid, "method": method, "path": path}
        if body is not None:
            frame["body"] = body
        try:
            await self._send(frame)
            return await asyncio.wait_for(fut, timeout=self._request_timeout)
        finally:
            self._pending.pop(rid, None)

    async def start_sync(self, body: dict[str, Any]) -> None:
        """Begin/replace the sliding-sync request. Resumes from the last
        persisted pos if we have one."""
        self._sync_body = body
        frame: dict[str, Any] = {"t": "sync_start", "body": body}
        # Resume BOTH cursors from durable storage; omit either only on a genuinely
        # fresh sync. The gateway keeps cursors only in memory for the socket, so the
        # device is the source of truth for both on reconnect.
        if self._last_persisted_pos is not None:
            frame["pos"] = self._last_persisted_pos
        if self._last_persisted_to_device_pos is not None:
            frame["to_device_pos"] = self._last_persisted_to_device_pos
        await self._send(frame)

    async def update_sync(self, body: dict[str, Any]) -> None:
        self._sync_body = body
        await self._send({"t": "sync_update", "body": body})

    async def ack_sync(self, pos: str, to_device_pos: str | None = None) -> None:
        """Tell the gateway the acked frame is durably persisted (timeline up to
        `pos` AND its to-device room keys + crypto state, with the to-device cursor
        at `to_device_pos`), so it may advance BOTH upstream cursors. CALL ONLY AFTER
        the crypto store is written (anti-UTD): never ack a to_device_pos whose keys
        aren't durable. Records both for reconnect resume; carries the last
        to_device_pos forward on frames that had no to-device section."""
        self._last_persisted_pos = pos
        frame: dict[str, Any] = {"t": "sync_ack", "pos": pos}
        if to_device_pos is not None:
            self._last_persisted_to_device_pos = to_device_pos
        # Send the latest to-device cursor on durable storage (carry-forward); omit
        # only if we've never received one (absent → gateway leaves it unchanged).
        if self._last_persisted_to_device_pos is not None:
            frame["to_device_pos"] = self._last_persisted_to_device_pos
        # Durably persist BOTH cursors (atomically, together) so a PROCESS restart —
        # not just a same-process reconnect — resumes incrementally (protocol D).
        # This runs on the ack path, i.e. AFTER the crypto driver flushed this
        # frame's room keys (receive_sync_changes) — so we never record a
        # to_device_pos ahead of its keys ("ack-only-after-durable").
        cursor_store = getattr(self, "_cursor_store", None)
        if cursor_store is not None:
            cursor_store.persist(self._last_persisted_pos, self._last_persisted_to_device_pos)
        await self._send(frame)

    # ─── run loop ─────────────────────────────────────────────────────────

    async def _run_forever(self) -> None:
        await run_with_reconnect(
            self._connect_once,
            abort_signal=self._abort,
            on_error=lambda e: logger.warning("gateway connection error: %s", e),
            on_reconnect=lambda d: logger.info("gateway reconnect in %.1fs", d),
        )

    async def _connect_once(self) -> None:
        self._authed.clear()
        async with websockets.connect(
            self._creds.gateway_url, max_size=4 * 1024 * 1024, ping_interval=25, ping_timeout=15
        ) as ws:
            self._ws = ws
            # Fresh sync queue + worker per connection (drop any stale frames
            # from a previous socket).
            self._sync_queue = asyncio.Queue()
            self._sync_worker = asyncio.ensure_future(self._sync_worker_loop())
            await self._send_auth(self._creds.access_token)
            try:
                async for raw in ws:
                    await self._dispatch(raw)
            finally:
                if self._sync_worker is not None:
                    self._sync_worker.cancel()
                    self._sync_worker = None
                self._ws = None
                self._fail_pending(ConnectionError("socket closed"))
                # Resume sync from our last persisted pos on the next connect.
                if self._sync_body is not None:
                    logger.info(
                        "gateway socket closed; will resume sync from pos=%s",
                        self._last_persisted_pos,
                    )

    async def _send_auth(self, token: str) -> None:
        await self._send(
            {
                "t": "auth",
                "access_token": token,
                "app_id": self._creds.app_id,
                "client_version": self._creds.client_version,
                "platform": self._creds.platform,
                "release_channel": self._creds.release_channel,
            }
        )

    async def _dispatch(self, raw: str | bytes) -> None:
        try:
            frame = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return
        t = frame.get("t")

        if t == "auth_ok":
            self.user_id = frame.get("user_id")
            self.device_id = frame.get("device_id")
            self._authed.set()
            logger.info("gateway auth_ok user=%s device=%s", self.user_id, self.device_id)
            if self._sync_body is not None:
                await self.start_sync(self._sync_body)
            return

        if t == "auth_error":
            self._authed.clear()
            raise AuthError(
                frame.get("reason", "auth_error"),
                frame.get("min_client_version"),
                frame.get("max_client_version"),
            )

        if t == "reauth":
            logger.info("gateway requested reauth")
            token = None
            if self._on_reauth is not None:
                token = await self._on_reauth()
            await self._send_auth(token or self._creds.access_token)
            return

        if t == "resp":
            rid = frame.get("id")
            fut = self._pending.get(rid)
            if fut is not None and not fut.done():
                fut.set_result((int(frame.get("status", 0)), frame.get("body") or {}))
            return

        if t == "error":
            logger.warning("gateway error frame: %s", frame.get("reason"))
            return

        if t == "sync_reset":
            self._handle_sync_reset(frame)
            return

        if t == "sync":
            # Hand off to the worker — do NOT await on_sync here. on_sync makes its
            # own requests (key upload/query, createRoom, …) and awaits their `resp`
            # frames, which are read by THIS loop; awaiting on_sync inline would
            # block the loop from ever reading those resps → deadlock (the 30s
            # connect timeout). Enqueue and keep reading.
            self._sync_queue.put_nowait(frame)
            return

    def _handle_sync_reset(self, frame: dict[str, Any]) -> None:
        """Handle a gateway→device `sync_reset` frame (protocol D.1 / D.2 cursor-expiry
        recovery). The homeserver expired the room cursor with `M_UNKNOWN_POS`; the
        gateway has ALREADY dropped the named cursor(s) and re-initialised the upstream
        sync from scratch on THIS SAME socket. Our job (D.2 "Device rule"):

          - immediately discard exactly the named durable cursor(s) so a later reconnect
            cannot replay them (for `pos_expired` the gateway names `["pos"]` only);
          - KEEP any cursor not named — a `pos_expired` reset leaves `to_device_pos`
            intact (it is a separate durable stream token, never invalidated; dropping
            it would lose Megolm keys);
          - do NOT tear down crypto state, and do NOT send a new `sync_start` — the fresh
            `sync` frames are already streaming on this socket and the sync worker keeps
            consuming them, persisting the new `pos` through the normal ack flow.

        Defensive parse: a missing/garbled `cursors` list is treated as empty (we clear
        nothing rather than guess), and an unexpected `cursors` shape is logged — a
        malformed reset must not crash the read loop."""
        reason = frame.get("reason")
        raw = frame.get("cursors")
        if isinstance(raw, list):
            names = [c for c in raw if isinstance(c, str)]
        else:
            names = []
            if raw is not None:
                logger.warning("sync_reset with non-list cursors (reason=%s): %r", reason, raw)
        logger.info("gateway sync_reset reason=%s cursors=%s", reason, names)
        if not names:
            return
        # Clear the named cursor(s) IN MEMORY so neither this socket's next ack-resume
        # nor a reconnect's sync_start replays the expired value; KEEP the rest.
        if "pos" in names:
            self._last_persisted_pos = None
        if "to_device_pos" in names:
            self._last_persisted_to_device_pos = None
        # Discard the same named cursor(s) from durable storage (atomically, keeping the
        # survivors) so a PROCESS restart can't replay them either.
        cursor_store = getattr(self, "_cursor_store", None)
        if cursor_store is not None:
            cursor_store.clear_cursors(names)

    async def _sync_worker_loop(self) -> None:
        """Process sync frames serially, OFF the read loop, so the read loop stays
        free to deliver the `resp` frames that on_sync's requests depend on."""
        while True:
            frame = await self._sync_queue.get()
            try:
                await self._on_sync(frame)
            except Exception as exc:  # noqa: BLE001
                # One bad sync frame must not kill the worker (and the whole
                # session). Report once to the sink, then keep draining.
                logger.warning("sync handler error: %s", exc)
                dump_chat4000_trace("gateway.sync_worker", exc)
            finally:
                self._sync_queue.task_done()

    async def _send(self, frame: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None:
            raise ConnectionError("gateway not connected")
        async with self._send_lock:
            await ws.send(json.dumps(frame, ensure_ascii=False))

    def _fail_pending(self, exc: Exception) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()
