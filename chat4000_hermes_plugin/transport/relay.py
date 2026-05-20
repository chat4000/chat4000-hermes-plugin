"""`RelayMessageTransport` — the production WebSocket-backed transport.

Port of clawconnect-plugin/src/transport/relay.ts (the heaviest TS file
in the plugin). Owns:

  - WebSocket lifecycle, hello + hello_ok handshake
  - XChaCha20-Poly1305 + outer envelope wrapping
  - §6.6 ack flow:
      * inbound parses outer `seq`, dedupes on inner.id, debounces
        cumulative `recv_ack` via RecvAckBatcher, persists watermark
      * outbound tracks msg_ids; surfaces `relay_recv_ack` as
        StatusUpdate(status='sent')
  - 25 s app-layer ping / 15 s pong-timeout reconnect (§6.5)
  - exponential-backoff reconnect via run_with_reconnect
  - outbound dedup: at most one ack per (refs, stage), one text_end per
    stream_id, one tool_end per tool_id

Hides from consumers: wire vocabulary, seq numbers, encryption, reconnect
bookkeeping, app-layer ping/pong.

Pairing is OUT of scope — see src/pairing.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Optional

import websockets
from websockets.client import WebSocketClientProtocol

from ..ack_store import Chat4000AckStore, open_ack_store
from ..crypto import decrypt, encrypt
from ..error_log import dump_chat4000_trace
from ..key_store import resolve_chat4000_instance_identity
from ..package_info import read_package_version
from ..reconnect import run_with_reconnect
from ..recv_ack_batcher import RecvAckBatcher, RecvAckBatcherOptions
from ..runtime_logger import RuntimeLogger
from ..protocol_types import (
    InnerMessage,
    InnerMessageFrom,
    OutboundAck,
    OutboundAudio,
    OutboundImage,
    OutboundMessage,
    OutboundStatus,
    OutboundText,
    OutboundTextDelta,
    OutboundTextEnd,
    OutboundToolDelta,
    OutboundToolEnd,
    OutboundToolStart,
    StatusUpdate,
)
from . import (
    ConnectionHandler,
    ConnectionStateUnion,
    GroupConfig,
    MessageTransport,
    ReceiveHandler,
    StatusHandler,
    Unsubscribe,
)

logger = logging.getLogger(__name__)

DEFAULT_RELAY_URL = "wss://relay.chat4000.com/ws"
APP_PING_INTERVAL_SECS = 25.0
APP_PONG_TIMEOUT_SECS = 15.0

PLUGIN_BUNDLE_ID = "@chat4000/hermes-plugin"


class RelayMessageTransport(MessageTransport):
    def __init__(
        self,
        *,
        abort_signal: Optional[asyncio.Event] = None,
        ack_store: Optional[Chat4000AckStore] = None,
    ):
        self._abort_signal = abort_signal
        self._store_override = ack_store

        self._receive_handlers: set[ReceiveHandler] = set()
        self._status_handlers: set[StatusHandler] = set()
        self._state_handlers: set[ConnectionHandler] = set()
        self._connection_state: ConnectionStateUnion = "disconnected"

        # Outbound dedup tables: (refs, stage) → wire id; stream_id → wire id;
        # tool_id → wire id (for tool_end only — start/delta don't dedup).
        self._ack_dedup: dict[str, str] = {}
        self._stream_ended_wire_id: dict[str, str] = {}
        self._tool_ended_wire_id: dict[str, str] = {}

        self._config: Optional[GroupConfig] = None
        self._store: Optional[Chat4000AckStore] = None
        self._from: Optional[InnerMessageFrom] = None
        self._runtime_logger: Optional[RuntimeLogger] = None
        self._current_send: Optional[Any] = None  # async fn: envelope dict -> None
        self._current_batcher: Optional[RecvAckBatcher] = None
        self._run_task: Optional[asyncio.Task] = None
        self._internal_abort: Optional[asyncio.Event] = None
        self._disposed = False

    # ─── MessageTransport surface ────────────────────────────────────────

    def send(self, msg: OutboundMessage) -> str:
        if self._disposed or self._config is None:
            wire_id = str(uuid.uuid4())
            reason = "disposed" if self._disposed else "not_connected"
            self._emit_status_async(StatusUpdate(msg_id=wire_id, status="failed", reason=reason))
            if self._runtime_logger is not None:
                self._runtime_logger.info(
                    "runtime.send_dropped", {"msg_id": wire_id, "reason": reason}
                )
            return wire_id

        if isinstance(msg, OutboundAck):
            return self._send_ack(msg)
        if isinstance(msg, OutboundTextEnd):
            return self._send_text_end(msg)
        if isinstance(msg, OutboundTextDelta):
            return self._ship_inner(
                "text_delta",
                {"delta": msg.delta, "stream_id": msg.stream_id},
                notify_if_offline=False,
            )
        if isinstance(msg, OutboundStatus):
            wire_id = self._ship_inner(
                "status", {"status": msg.status}, notify_if_offline=False
            )
            if self._runtime_logger is not None:
                self._runtime_logger.info(
                    "runtime.send", {"type": "status", "msg_id": wire_id, "status": msg.status}
                )
            return wire_id
        if isinstance(msg, OutboundText):
            return self._ship_inner(
                "text", {"text": msg.text}, notify_if_offline=True
            )
        if isinstance(msg, OutboundImage):
            import base64

            return self._ship_inner(
                "image",
                {
                    "data_base64": base64.b64encode(msg.data).decode("ascii"),
                    "mime_type": msg.mime_type,
                },
                notify_if_offline=True,
            )
        if isinstance(msg, OutboundAudio):
            import base64

            return self._ship_inner(
                "audio",
                {
                    "data_base64": base64.b64encode(msg.data).decode("ascii"),
                    "mime_type": msg.mime_type,
                    "duration_ms": msg.duration_ms,
                    "waveform": msg.waveform,
                },
                notify_if_offline=True,
            )
        # Tool-call frames — Hermes-specific. notify_if_offline=False because
        # tools don't justify a silent-push wake on their own.
        if isinstance(msg, OutboundToolStart):
            body: dict = {
                "tool_id": msg.tool_id,
                "name": msg.name,
                "args": msg.args,
            }
            # `icon` is additive (added 2026-05-20). Only include when set
            # so the wire frame stays minimal for tools without a registered
            # emoji — older receivers ignore unknown fields anyway.
            if msg.icon:
                body["icon"] = msg.icon
            logger.info(
                "transport.send tool_start tool_id=%s name=%s icon=%r args_len=%d",
                msg.tool_id, msg.name, msg.icon, len(msg.args),
            )
            return self._ship_inner("tool_start", body, notify_if_offline=False)
        if isinstance(msg, OutboundToolDelta):
            return self._ship_inner(
                "tool_delta",
                {"tool_id": msg.tool_id, "delta": msg.delta},
                notify_if_offline=False,
            )
        if isinstance(msg, OutboundToolEnd):
            cached = self._tool_ended_wire_id.get(msg.tool_id)
            if cached is not None:
                return cached
            wire_id = self._ship_inner(
                "tool_end",
                {
                    "tool_id": msg.tool_id,
                    "status": msg.status,
                    "result": msg.result,
                    "duration_ms": msg.duration_ms,
                },
                notify_if_offline=False,
            )
            self._tool_ended_wire_id[msg.tool_id] = wire_id
            return wire_id

        raise TypeError(f"unsupported OutboundMessage: {type(msg).__name__}")

    def on_receive(self, handler: ReceiveHandler) -> Unsubscribe:
        self._receive_handlers.add(handler)
        return lambda: self._receive_handlers.discard(handler)

    def on_status(self, handler: StatusHandler) -> Unsubscribe:
        self._status_handlers.add(handler)
        return lambda: self._status_handlers.discard(handler)

    def on_connection_state(self, handler: ConnectionHandler) -> Unsubscribe:
        self._state_handlers.add(handler)
        try:
            handler(self._connection_state)
        except Exception:
            pass
        return lambda: self._state_handlers.discard(handler)

    def connect(self, config: GroupConfig) -> None:
        if self._disposed:
            raise RuntimeError("RelayMessageTransport: connect() after disconnect()")
        if self._config is not None:
            return  # idempotent
        self._config = config
        self._runtime_logger = RuntimeLogger(
            config.runtime_log_level, account_id=config.account_id, group_id=config.group_id
        )
        self._store = self._store_override or open_ack_store(config.account_id)
        self._internal_abort = asyncio.Event()
        if self._abort_signal is not None:
            # Chain external abort into our internal one.
            external = self._abort_signal

            async def _chain() -> None:
                await external.wait()
                if self._internal_abort is not None:
                    self._internal_abort.set()

            asyncio.ensure_future(_chain())
        self._run_task = asyncio.ensure_future(self._start_run_loop())

    async def disconnect(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        if self._current_batcher is not None:
            try:
                self._current_batcher.shutdown()
            except Exception:
                pass
            self._current_batcher = None
        if self._internal_abort is not None:
            self._internal_abort.set()
        self._set_state("disconnected")
        if self._run_task is not None:
            try:
                await asyncio.wait_for(self._run_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                self._run_task.cancel()

    # ─── Internals ───────────────────────────────────────────────────────

    def _resolve_from(self) -> InnerMessageFrom:
        if self._from is None:
            instance = resolve_chat4000_instance_identity()
            self._from = InnerMessageFrom(
                role="plugin",
                device_id=instance.device_id,
                device_name=instance.device_name,
                app_version=read_package_version(),
                bundle_id=PLUGIN_BUNDLE_ID,
            )
        return self._from

    def _send_ack(self, msg: OutboundAck) -> str:
        key = f"{msg.refs}::{msg.stage}"
        cached = self._ack_dedup.get(key)
        if cached is not None:
            return cached
        persisted = (
            self._store.mark_inner_ack_emitted(
                group_id=self._config.group_id,  # type: ignore[union-attr]
                refs=msg.refs,
                stage=msg.stage,
            )
            if self._store is not None
            else None
        )
        if persisted is not None and not persisted.is_new:
            # Already emitted in a prior process. Suppress the wire frame but
            # give the caller a stable id.
            wire_id = str(uuid.uuid4())
            self._ack_dedup[key] = wire_id
            return wire_id
        wire_id = self._ship_inner(
            "ack",
            {"refs": msg.refs, "stage": msg.stage},
            notify_if_offline=False,
        )
        self._ack_dedup[key] = wire_id
        if self._runtime_logger is not None:
            self._runtime_logger.info(
                "runtime.inner_ack_emit",
                {"msg_id": wire_id, "refs": msg.refs, "stage": msg.stage},
            )
        return wire_id

    def _send_text_end(self, msg: OutboundTextEnd) -> str:
        cached = self._stream_ended_wire_id.get(msg.stream_id)
        if cached is not None:
            return cached
        body: dict = {"text": msg.text, "stream_id": msg.stream_id}
        if msg.reset:
            body["reset"] = True
        wire_id = self._ship_inner(
            "text_end", body, notify_if_offline=(not msg.reset)
        )
        self._stream_ended_wire_id[msg.stream_id] = wire_id
        return wire_id

    def _ship_inner(
        self, t: str, body: dict, *, notify_if_offline: bool
    ) -> str:
        if self._config is None or self._store is None:
            raise RuntimeError("RelayMessageTransport: not connected")
        wire_id = str(uuid.uuid4())
        from_ = self._resolve_from()
        inner = InnerMessage(
            t=t,  # type: ignore[arg-type]
            id=wire_id,
            from_=from_,
            body=body,
            ts=int(time.time() * 1000),
        )
        plaintext = json.dumps(inner.to_wire(), ensure_ascii=False).encode("utf-8")
        nonce_b64, ciphertext_b64 = encrypt(plaintext, self._config.group_key_bytes)
        payload: dict[str, Any] = {
            "msg_id": wire_id,
            "nonce": nonce_b64,
            "ciphertext": ciphertext_b64,
        }
        if notify_if_offline:
            payload["notify_if_offline"] = True
        envelope = {"version": 1, "type": "msg", "payload": payload}

        send = self._current_send
        if send is None:
            self._emit_status_async(
                StatusUpdate(msg_id=wire_id, status="failed", reason="not connected")
            )
            if self._runtime_logger is not None:
                self._runtime_logger.info(
                    "runtime.send_dropped",
                    {"msg_id": wire_id, "inner_t": t, "reason": "not_connected"},
                )
            return wire_id
        try:
            result = send(envelope)
            if asyncio.iscoroutine(result):
                asyncio.ensure_future(result)
        except Exception as e:
            self._emit_status_async(
                StatusUpdate(msg_id=wire_id, status="failed", reason=str(e))
            )
        if self._runtime_logger is not None:
            self._runtime_logger.info(
                "runtime.send", {"type": "msg", "msg_id": wire_id, "inner_t": t}
            )
        return wire_id

    async def _start_run_loop(self) -> None:
        if self._config is None or self._internal_abort is None or self._store is None:
            return
        config = self._config

        async def connect_once() -> None:
            assert self._store is not None
            last_acked_seq = self._store.get_last_acked_seq(config.group_id, "plugin")
            self._set_state("connecting")
            await self._run_one_connection(
                relay_url=config.relay_url or DEFAULT_RELAY_URL,
                group_id=config.group_id,
                group_key_bytes=config.group_key_bytes,
                release_channel=config.release_channel or "production",
                last_acked_seq=last_acked_seq,
            )

        await run_with_reconnect(
            connect_once,
            abort_signal=self._internal_abort,
            on_error=lambda exc: (
                dump_chat4000_trace(
                    "relay-transport", exc, {"account_id": config.account_id}
                ),
                self._runtime_logger.info(
                    "runtime.relay_error", {"error": str(exc)}
                ) if self._runtime_logger else None,
                self._set_state("reconnecting"),
            )[-1] if False else None,
            on_reconnect=lambda delay: self._runtime_logger.info(
                "runtime.reconnect", {"delay_ms": int(delay * 1000)}
            ) if self._runtime_logger else None,
        )

    async def _run_one_connection(
        self,
        *,
        relay_url: str,
        group_id: str,
        group_key_bytes: bytes,
        release_channel: str,
        last_acked_seq: int,
    ) -> None:
        opened = False
        last_send_at = time.time()
        ping_task: Optional[asyncio.Task] = None
        pong_timer: Optional[asyncio.TimerHandle] = None

        async with websockets.connect(
            relay_url, max_size=4 * 1024 * 1024
        ) as ws:
            send_lock = asyncio.Lock()

            async def send_envelope(envelope: dict) -> None:
                nonlocal last_send_at
                async with send_lock:
                    if ws.state.name in ("OPEN",):  # avoid send-after-close
                        await ws.send(json.dumps(envelope))
                        last_send_at = time.time()

            # Send hello immediately.
            hello_payload: dict[str, Any] = {
                "role": "plugin",
                "group_id": group_id,
                "device_token": None,
                "app_version": read_package_version(),
                "release_channel": release_channel,
            }
            if last_acked_seq > 0:
                hello_payload["last_acked_seq"] = last_acked_seq
            await send_envelope(
                {"version": 1, "type": "hello", "payload": hello_payload}
            )
            if self._runtime_logger is not None:
                self._runtime_logger.info(
                    "runtime.hello_sent", {"last_acked_seq": last_acked_seq}
                )

            async def app_ping_loop() -> None:
                nonlocal pong_timer
                while True:
                    await asyncio.sleep(APP_PING_INTERVAL_SECS)
                    if ws.state.name != "OPEN":
                        return
                    if time.time() - last_send_at < APP_PING_INTERVAL_SECS - 1:
                        continue
                    try:
                        await send_envelope(
                            {"version": 1, "type": "ping", "payload": None}
                        )
                    except Exception:
                        return
                    # Schedule pong timeout — close socket if not received.
                    if pong_timer is not None:
                        pong_timer.cancel()
                    loop = asyncio.get_running_loop()

                    def _close_on_pong_timeout() -> None:
                        try:
                            asyncio.ensure_future(ws.close())
                        except Exception:
                            pass

                    pong_timer = loop.call_later(
                        APP_PONG_TIMEOUT_SECS, _close_on_pong_timeout
                    )

            try:
                async for raw_frame in ws:
                    if not raw_frame:
                        continue
                    try:
                        envelope = json.loads(raw_frame)
                    except (json.JSONDecodeError, ValueError):
                        continue

                    env_type = envelope.get("type")

                    if env_type == "hello_ok":
                        opened = True
                        self._current_send = send_envelope
                        assert self._store is not None
                        self._current_batcher = RecvAckBatcher(
                            RecvAckBatcherOptions(
                                group_id=group_id,
                                store=self._store,
                                send=send_envelope,
                                role="plugin",
                            )
                        )
                        ping_task = asyncio.ensure_future(app_ping_loop())
                        self._set_state("connected")
                        if self._runtime_logger is not None:
                            payload = envelope.get("payload") or {}
                            self._runtime_logger.info(
                                "runtime.hello_ok",
                                {"current_terms_version": payload.get("current_terms_version")},
                            )
                        continue

                    if env_type == "hello_error":
                        payload = envelope.get("payload") or {}
                        raise RuntimeError(
                            f"relay rejected hello: {payload.get('code')} — {payload.get('message')}"
                        )

                    if env_type == "msg":
                        await self._handle_inbound_msg(
                            envelope.get("payload") or {},
                            group_key_bytes=group_key_bytes,
                            group_id=group_id,
                        )
                        continue

                    if env_type == "ping":
                        try:
                            await send_envelope(
                                {"version": 1, "type": "pong", "payload": None}
                            )
                        except Exception:
                            pass
                        continue

                    if env_type == "pong":
                        if pong_timer is not None:
                            pong_timer.cancel()
                            pong_timer = None
                        continue

                    if env_type == "relay_recv_ack":
                        payload = envelope.get("payload") or {}
                        msg_id = payload.get("msg_id")
                        if isinstance(msg_id, str) and msg_id:
                            self._emit_status_async(
                                StatusUpdate(msg_id=msg_id, status="sent")
                            )
                        if self._runtime_logger is not None:
                            self._runtime_logger.info(
                                "runtime.relay_recv_ack", {"msg_id": msg_id}
                            )
                        continue
            finally:
                if ping_task is not None:
                    ping_task.cancel()
                if pong_timer is not None:
                    pong_timer.cancel()
                if self._current_batcher is not None:
                    try:
                        self._current_batcher.shutdown()
                    except Exception:
                        pass
                self._current_batcher = None
                self._current_send = None
                if not opened:
                    raise RuntimeError("WebSocket closed before hello_ok")
                self._set_state("reconnecting")

    async def _handle_inbound_msg(
        self, msg: dict, *, group_key_bytes: bytes, group_id: str
    ) -> None:
        nonce = msg.get("nonce")
        ciphertext = msg.get("ciphertext")
        outer_msg_id = msg.get("msg_id")
        seq = msg.get("seq")
        if self._runtime_logger is not None:
            self._runtime_logger.info(
                "runtime.recv", {"type": "msg", "msg_id": outer_msg_id, "seq": seq}
            )

        if not isinstance(nonce, str) or not isinstance(ciphertext, str):
            return
        plaintext = decrypt(nonce, ciphertext, group_key_bytes)
        if plaintext is None:
            if self._runtime_logger is not None:
                self._runtime_logger.info(
                    "runtime.msg_decrypt_error",
                    {"msg_id": outer_msg_id, "seq": seq},
                )
            return

        try:
            wire = json.loads(plaintext.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            if self._runtime_logger is not None:
                self._runtime_logger.info(
                    "runtime.msg_parse_error",
                    {"msg_id": outer_msg_id, "seq": seq},
                )
            self._queue_recv_ack(seq)
            return

        inner_id = wire.get("id")
        if not isinstance(inner_id, str) or not inner_id:
            self._queue_recv_ack(seq)
            return

        # §6.6.9 dedup on inner.id (NOT outer seq).
        assert self._store is not None
        result = self._store.mark_processed(group_id, inner_id)
        if not result.is_new:
            if self._runtime_logger is not None:
                self._runtime_logger.info(
                    "runtime.msg_dedup",
                    {"msg_id": outer_msg_id, "inner_id": inner_id, "seq": seq,
                     "inner_t": wire.get("t")},
                )
            self._queue_recv_ack(seq)
            return

        consumer_inner = _to_consumer_inner(wire)
        if consumer_inner is None:
            if self._runtime_logger is not None:
                self._runtime_logger.info(
                    "runtime.msg_dropped",
                    {"msg_id": outer_msg_id, "inner_id": inner_id,
                     "reason": "unrecognized_inner_t", "inner_t": wire.get("t")},
                )
            self._queue_recv_ack(seq)
            return

        if self._runtime_logger is not None:
            self._runtime_logger.info(
                "runtime.inner_parsed",
                {
                    "msg_id": outer_msg_id, "inner_id": inner_id,
                    "inner_t": consumer_inner.t,
                    "from_role": consumer_inner.from_.role if consumer_inner.from_ else None,
                },
            )

        try:
            for handler in list(self._receive_handlers):
                result = handler(consumer_inner)
                if asyncio.iscoroutine(result):
                    await result
        finally:
            self._queue_recv_ack(seq)

    def _queue_recv_ack(self, seq: Any) -> None:
        if not isinstance(seq, int) or seq <= 0:
            return
        if self._current_batcher is None:
            return
        self._current_batcher.record_persisted(seq)

    def _emit_status_async(self, update: StatusUpdate) -> None:
        try:
            loop = asyncio.get_running_loop()
            loop.call_soon(self._emit_status, update)
        except RuntimeError:
            self._emit_status(update)

    def _emit_status(self, update: StatusUpdate) -> None:
        for handler in list(self._status_handlers):
            try:
                result = handler(update)
                if asyncio.iscoroutine(result):
                    asyncio.ensure_future(result)
            except Exception:
                pass

    def _set_state(self, state: ConnectionStateUnion) -> None:
        self._connection_state = state
        for handler in list(self._state_handlers):
            try:
                result = handler(state)
                if asyncio.iscoroutine(result):
                    asyncio.ensure_future(result)
            except Exception:
                pass


def _to_consumer_inner(wire: dict) -> Optional[InnerMessage]:
    """Convert a parsed wire dict into a typed InnerMessage. Returns None
    for unrecognized inner types (forward-compat — older receivers ignore
    new types like tool_start/tool_delta/tool_end)."""
    t = wire.get("t")
    if t not in (
        "text", "image", "audio", "text_delta", "text_end", "status", "ack",
        "tool_start", "tool_delta", "tool_end",
    ):
        return None
    from_raw = wire.get("from")
    from_obj: Optional[InnerMessageFrom] = None
    if isinstance(from_raw, dict):
        from_obj = InnerMessageFrom(
            role=from_raw.get("role", "plugin"),
            device_id=from_raw.get("device_id"),
            device_name=from_raw.get("device_name"),
            app_version=from_raw.get("app_version"),
            bundle_id=from_raw.get("bundle_id"),
        )
    return InnerMessage(
        t=t,  # type: ignore[arg-type]
        id=wire.get("id", ""),
        from_=from_obj,
        body=wire.get("body") or {},
        ts=int(wire.get("ts") or (time.time() * 1000)),
    )
