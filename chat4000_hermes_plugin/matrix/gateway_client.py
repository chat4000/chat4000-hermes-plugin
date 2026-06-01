"""WS Gateway client — the single WebSocket the plugin holds (protocol D).

Speaks the gateway's muxed frame envelope (verified against the real
`chat4000-matrix-ws-proxy/src/protocol.rs`):

  client → gateway:  auth, sync_start, sync_update, sync_ack, sync_stop, req
  gateway → client:  auth_ok, auth_error, reauth, resp, error, sync

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
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

import websockets

from ..reconnect import run_with_reconnect

logger = logging.getLogger(__name__)

SyncHandler = Callable[[dict], Awaitable[None]]
ReauthHandler = Callable[[], Awaitable[Optional[str]]]  # returns a fresh token or None


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

    def __init__(self, reason: str, min_v: Optional[str], max_v: Optional[str]):
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
        on_reauth: Optional[ReauthHandler] = None,
        request_timeout: float = 30.0,
        abort_signal: Optional[asyncio.Event] = None,
    ):
        self._creds = creds
        self._on_sync = on_sync
        self._on_reauth = on_reauth
        self._request_timeout = request_timeout
        self._abort = abort_signal or asyncio.Event()

        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._pending: dict[str, asyncio.Future] = {}
        self._send_lock = asyncio.Lock()

        # Sliding-sync state the CLIENT owns (the gateway keeps pos only in
        # memory for the socket; on reconnect we resend our last persisted pos).
        self._sync_body: Optional[dict] = None
        self._last_persisted_pos: Optional[str] = None

        self.user_id: Optional[str] = None
        self.device_id: Optional[str] = None
        self._run_task: Optional[asyncio.Task] = None
        self._authed = asyncio.Event()
        # Sync frames are handled on a SEPARATE worker, never inline in the read
        # loop — see _sync_worker_loop for why (deadlock avoidance).
        self._sync_queue: asyncio.Queue = asyncio.Queue()
        self._sync_worker: Optional[asyncio.Task] = None

    # ─── lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the reconnecting run loop. Returns immediately; await
        `wait_authed()` to block until the first successful auth."""
        self._run_task = asyncio.ensure_future(self._run_forever())

    async def wait_authed(self) -> None:
        await self._authed.wait()

    async def close(self) -> None:
        self._abort.set()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._run_task is not None:
            try:
                await asyncio.wait_for(self._run_task, timeout=2.0)
            except Exception:
                self._run_task.cancel()

    # ─── public API used by the crypto driver / room layer ────────────────

    async def request(
        self, method: str, path: str, body: Optional[dict] = None
    ) -> tuple[int, dict]:
        """Forward a C-S API call. Returns `(status, body)`. This is THE way
        every homeserver call leaves the plugin."""
        await self._authed.wait()
        rid = uuid.uuid4().hex[:32]
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        frame: dict[str, Any] = {"t": "req", "id": rid, "method": method, "path": path}
        if body is not None:
            frame["body"] = body
        try:
            await self._send(frame)
            return await asyncio.wait_for(fut, timeout=self._request_timeout)
        finally:
            self._pending.pop(rid, None)

    async def start_sync(self, body: dict) -> None:
        """Begin/replace the sliding-sync request. Resumes from the last
        persisted pos if we have one."""
        self._sync_body = body
        frame: dict[str, Any] = {"t": "sync_start", "body": body}
        if self._last_persisted_pos is not None:
            frame["pos"] = self._last_persisted_pos
        await self._send(frame)

    async def update_sync(self, body: dict) -> None:
        self._sync_body = body
        await self._send({"t": "sync_update", "body": body})

    async def ack_sync(self, pos: str) -> None:
        """Tell the gateway the batch up to `pos` is durably persisted, so it may
        advance the upstream cursor. CALL ONLY AFTER the crypto store is written
        (anti-UTD). Also records `pos` for reconnect resume."""
        self._last_persisted_pos = pos
        await self._send({"t": "sync_ack", "pos": pos})

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
                    logger.info("gateway socket closed; will resume sync from pos=%s",
                                self._last_persisted_pos)

    async def _send_auth(self, token: str) -> None:
        await self._send({
            "t": "auth",
            "access_token": token,
            "app_id": self._creds.app_id,
            "client_version": self._creds.client_version,
            "platform": self._creds.platform,
            "release_channel": self._creds.release_channel,
        })

    async def _dispatch(self, raw: Any) -> None:
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

        if t == "sync":
            # Hand off to the worker — do NOT await on_sync here. on_sync makes its
            # own requests (key upload/query, createRoom, …) and awaits their `resp`
            # frames, which are read by THIS loop; awaiting on_sync inline would
            # block the loop from ever reading those resps → deadlock (the 30s
            # connect timeout). Enqueue and keep reading.
            self._sync_queue.put_nowait(frame)
            return

    async def _sync_worker_loop(self) -> None:
        """Process sync frames serially, OFF the read loop, so the read loop stays
        free to deliver the `resp` frames that on_sync's requests depend on."""
        while True:
            frame = await self._sync_queue.get()
            try:
                await self._on_sync(frame)
            except Exception as exc:  # noqa: BLE001
                logger.warning("sync handler error: %s", exc)
            finally:
                self._sync_queue.task_done()

    async def _send(self, frame: dict) -> None:
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
