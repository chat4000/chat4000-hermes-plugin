"""Pairing — joiner-side and initiator-side state machines.

Port of clawconnect-plugin/src/pairing.ts. The relay's pairing-room
protocol is unchanged from the OpenClaw days (§6.2), so we can reuse
the same proofs and the same wire shapes byte-for-byte.

Initiator (host) flow:
  1. open WS, send pair_open(role=initiator, room_id)
  2. wait pair_open_ok → pair_ready
  3. send pair_data{t:hello, salt}
  4. recv pair_data{t:join, salt=joiner_pub} → recv pair_data{t:proof_b}
  5. verify proof_b; if ok, wrap group_key with X25519+XChaCha → send
     pair_data{t:grant, proof, wrapped_key}
  6. recv pair_complete → close

Joiner flow:
  1. open WS, send pair_open(role=joiner, room_id)
  2. wait pair_open_ok / pair_ready
  3. recv pair_data{t:hello, salt} → derive proofs locally, send
     pair_data{t:join, salt=our_pub}, then pair_data{t:proof_b}
  4. recv pair_data{t:grant} → verify proof_a, unwrap group_key
  5. send pair_complete → close
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal, Optional

import websockets

from .crypto import (
    compute_pairing_proof,
    derive_group_id,
    derive_pairing_room_id,
    generate_pairing_code,
    generate_pairing_joiner_keypair,
    normalize_pairing_code,
    unwrap_group_key_from_initiator,
    wrap_group_key_to_joiner,
)
from .error_log import dump_chat4000_trace
from .pairing_logger import PairingLogger, PairingLogLevel
from .protocol_types import RelayWrappedKeyPayload

logger = logging.getLogger(__name__)

PairHostStatus = Literal[
    "connecting", "connected", "waiting", "joiner-ready", "grant-sent", "completed", "closed"
]


# ─── Joiner ───────────────────────────────────────────────────────────────


@dataclass
class PairJoinResult:
    group_key_bytes: bytes
    group_id: str


@dataclass
class PairJoinOptions:
    relay_url: str
    code: str
    log_level: PairingLogLevel = "info"
    abort_signal: Optional[asyncio.Event] = None


async def join_pairing_session(opts: PairJoinOptions) -> PairJoinResult:
    normalized_code = normalize_pairing_code(opts.code)
    if len(normalized_code) != 8:
        raise ValueError(
            f"pairing code must normalize to 8 chars; got {len(normalized_code)}"
        )
    room_id = derive_pairing_room_id(normalized_code)
    keypair = generate_pairing_joiner_keypair()
    log = PairingLogger(opts.log_level, room_id=room_id, code=opts.code)

    a_salt_b64: str = ""

    async with websockets.connect(opts.relay_url, max_size=4 * 1024 * 1024) as ws:
        log.info("pair.ws_open")

        async def send(envelope: dict, fields: Optional[dict] = None) -> None:
            log.log_send(envelope, fields)
            await ws.send(json.dumps(envelope))

        await send(
            {
                "version": 1,
                "type": "pair_open",
                "payload": {"role": "joiner", "room_id": room_id},
            }
        )

        try:
            async for raw_frame in ws:
                envelope = _parse_envelope(raw_frame)
                if envelope is None:
                    continue
                log.log_recv(envelope)
                env_type = envelope.get("type")

                if env_type == "pair_cancel":
                    payload = envelope.get("payload") or {}
                    log.log_cancel_remote(payload)
                    log.log_finish("cancel", payload.get("reason") or "remote_cancel")
                    raise RuntimeError("Pairing cancelled")

                if env_type in ("pair_open_ok", "pair_ready"):
                    if env_type == "pair_ready":
                        log.info("pair.ready")
                    continue

                if env_type != "pair_data":
                    continue

                payload = envelope.get("payload") or {}
                t = payload.get("t")

                if t == "hello":
                    a_salt_b64 = payload.get("salt") or ""
                    log.info(
                        "pair.recv_hello",
                        {"a_salt_len": len(_b64_decode_safe(a_salt_b64))},
                    )
                    # Send our pubkey and the proof_b in two frames.
                    await send(
                        {
                            "version": 1,
                            "type": "pair_data",
                            "payload": {"t": "join", "salt": keypair.public_key_base64},
                        },
                        {"joiner_pub_len": len(keypair.public_key)},
                    )
                    proof_b = compute_pairing_proof(
                        normalized_code,
                        a_salt_b64,
                        keypair.public_key_base64,
                        "B",
                    )
                    await send(
                        {
                            "version": 1,
                            "type": "pair_data",
                            "payload": {"t": "proof_b", "proof": proof_b},
                        }
                    )
                    continue

                if t == "grant":
                    if not a_salt_b64:
                        raise RuntimeError("received pair grant before hello salt")
                    expected = compute_pairing_proof(
                        normalized_code, a_salt_b64, keypair.public_key_base64, "A"
                    )
                    incoming_proof = payload.get("proof")
                    if incoming_proof != expected:
                        log.info("pair.proof_a_verify", {"verified": False})
                        log.log_finish("error", "pairing_proof_mismatch")
                        raise RuntimeError("pairing proof mismatch")
                    log.info("pair.proof_a_verify", {"verified": True})

                    wrapped_raw = payload.get("wrapped_key") or {}
                    wrapped = RelayWrappedKeyPayload(
                        ephemeral_pub=wrapped_raw.get("ephemeral_pub", ""),
                        nonce=wrapped_raw.get("nonce", ""),
                        ciphertext=wrapped_raw.get("ciphertext", ""),
                    )
                    group_key = unwrap_group_key_from_initiator(
                        wrapped, keypair.private_key
                    )
                    if group_key is None:
                        log.log_finish("error", "unwrap_failed")
                        raise RuntimeError("failed to unwrap group key")

                    await send(
                        {
                            "version": 1,
                            "type": "pair_complete",
                            "payload": {"status": "ok"},
                        }
                    )
                    await asyncio.sleep(0.3)  # let the relay process complete
                    log.log_finish("success", "pair_complete_sent")
                    return PairJoinResult(
                        group_key_bytes=group_key,
                        group_id=derive_group_id(group_key),
                    )
        except websockets.ConnectionClosed as exc:
            log.log_ws_close(exc.code, str(exc.reason))
            log.log_finish("error", f"socket closed ({exc.code})")
            dump_chat4000_trace("pair-join-close", exc, {"room_id": room_id})
            raise RuntimeError(f"Pairing socket closed ({exc.code})") from exc
        except Exception as exc:
            dump_chat4000_trace("pair-join-error", exc, {"room_id": room_id})
            raise

    raise RuntimeError("pairing stream ended without grant")


# ─── Initiator (host) ─────────────────────────────────────────────────────


@dataclass
class PairHostResult:
    code: str
    room_id: str


@dataclass
class PairHostOptions:
    relay_url: str
    group_key_bytes: bytes
    code: Optional[str] = None
    log_level: PairingLogLevel = "info"
    reconnect_delay_secs: float = 1.0
    on_status: Optional[Callable[[PairHostStatus, str], None]] = None
    abort_signal: Optional[asyncio.Event] = None


async def host_pairing_session(opts: PairHostOptions) -> PairHostResult:
    """Single-attempt host. Higher-level callers wrap this in a reconnect
    loop with `host_pairing_session_continuous` for App-Store-review-style
    repeated pairings."""
    from . import analytics

    code = (opts.code or "").strip() or generate_pairing_code()
    normalized_code = normalize_pairing_code(code)
    room_id = derive_pairing_room_id(normalized_code)
    analytics.track("pairing_started", {"flow": "host"})
    a_salt = secrets.token_bytes(32)
    import base64

    a_salt_b64 = base64.b64encode(a_salt).decode("ascii")
    log = PairingLogger(opts.log_level, room_id=room_id, code=code)

    joiner_public_key_b64: Optional[str] = None
    grant_sent = False

    def emit_status(status: PairHostStatus, detail: str) -> None:
        if opts.on_status is not None:
            try:
                opts.on_status(status, detail)
            except Exception:
                pass

    emit_status("connecting", "Connecting to relay")

    try:
        async with websockets.connect(opts.relay_url, max_size=4 * 1024 * 1024) as ws:
            log.info("pair.ws_open")

            async def send(envelope: dict, fields: Optional[dict] = None) -> None:
                log.log_send(envelope, fields)
                await ws.send(json.dumps(envelope))

            await send(
                {
                    "version": 1,
                    "type": "pair_open",
                    "payload": {"role": "initiator", "room_id": room_id},
                }
            )

            async for raw_frame in ws:
                envelope = _parse_envelope(raw_frame)
                if envelope is None:
                    continue
                log.log_recv(envelope)
                env_type = envelope.get("type")

                if env_type == "pair_open_ok":
                    emit_status("connected", "Connected to relay")
                    emit_status("waiting", "Waiting for client to join")
                    continue

                if env_type == "pair_ready":
                    log.info("pair.ready")
                    emit_status("joiner-ready", "Client joined pairing session")
                    await send(
                        {
                            "version": 1,
                            "type": "pair_data",
                            "payload": {"t": "hello", "salt": a_salt_b64},
                        },
                        {"a_salt_len": len(a_salt)},
                    )
                    continue

                if env_type == "pair_cancel":
                    payload = envelope.get("payload") or {}
                    log.log_cancel_remote(payload)
                    log.log_finish("cancel", payload.get("reason") or "remote_cancel")
                    raise RuntimeError("Pairing cancelled")

                if env_type == "pair_complete":
                    log.log_finish("success", "pair_complete_received")
                    emit_status("completed", "Pairing complete")
                    analytics.track("pairing_completed", {"flow": "host"})
                    analytics.flush()
                    return PairHostResult(code=code, room_id=room_id)

                if env_type != "pair_data":
                    continue

                payload = envelope.get("payload") or {}
                t = payload.get("t")

                if t == "join":
                    joiner_public_key_b64 = payload.get("salt") or ""
                    log.info(
                        "pair.join_received",
                        {"joiner_pub_len": len(_b64_decode_safe(joiner_public_key_b64))},
                    )
                    continue

                if t == "proof_b":
                    log.info("pair.proof_b_received")
                    if not joiner_public_key_b64:
                        log.log_finish("error", "proof_before_join")
                        raise RuntimeError("received proof before join")
                    expected = compute_pairing_proof(
                        normalized_code, a_salt_b64, joiner_public_key_b64, "B"
                    )
                    actual = payload.get("proof")
                    if actual != expected:
                        log.info(
                            "pair.proof_b_verify", {"verified": False}
                        )
                        log.log_cancel_local("proof_mismatch")
                        await send(
                            {
                                "version": 1,
                                "type": "pair_cancel",
                                "payload": {"reason": "proof_mismatch"},
                            },
                            {"cancel_origin": "local"},
                        )
                        log.log_finish("error", "joiner_proof_mismatch")
                        raise RuntimeError("joiner proof mismatch")
                    log.info("pair.proof_b_verify", {"verified": True})

                    wrapped = wrap_group_key_to_joiner(
                        joiner_public_key_b64, opts.group_key_bytes
                    )
                    proof_a = compute_pairing_proof(
                        normalized_code, a_salt_b64, joiner_public_key_b64, "A"
                    )
                    await send(
                        {
                            "version": 1,
                            "type": "pair_data",
                            "payload": {
                                "t": "grant",
                                "proof": proof_a,
                                "wrapped_key": {
                                    "ephemeral_pub": wrapped.ephemeral_pub,
                                    "nonce": wrapped.nonce,
                                    "ciphertext": wrapped.ciphertext,
                                },
                            },
                        },
                        {
                            "ephemeral_pub_len": len(_b64_decode_safe(wrapped.ephemeral_pub)),
                            "nonce_len": len(_b64_decode_safe(wrapped.nonce)),
                            "ciphertext_len": len(_b64_decode_safe(wrapped.ciphertext)),
                        },
                    )
                    grant_sent = True
                    log.info(
                        "pair.grant_sent",
                        {"ephemeral_pub_len": len(_b64_decode_safe(wrapped.ephemeral_pub))},
                    )
                    emit_status("grant-sent", "Transferred encrypted group key")
                    continue

    except websockets.ConnectionClosed as exc:
        if grant_sent:
            # The client may close the socket right after `pair_complete` —
            # treat that as success since the key has already been wrapped.
            log.log_ws_close(exc.code, str(exc.reason))
            log.log_finish("success", "closed_after_grant")
            emit_status("completed", "Pairing room closed after key transfer")
            analytics.track("pairing_completed", {"flow": "host", "via": "closed_after_grant"})
            analytics.flush()
            return PairHostResult(code=code, room_id=room_id)
        detail = f"Pairing socket closed ({exc.code})"
        log.log_ws_close(exc.code, str(exc.reason))
        log.log_finish("error", detail)
        dump_chat4000_trace("pair-host-close", exc, {"room_id": room_id})
        emit_status("closed", detail)
        analytics.track("pairing_failed", {"flow": "host", "reason": "socket_closed", "ws_code": exc.code})
        analytics.flush()
        raise RuntimeError(detail) from exc

    analytics.track("pairing_failed", {"flow": "host", "reason": "stream_ended_without_complete"})
    analytics.flush()
    raise RuntimeError("pairing stream ended without complete")


@dataclass
class ContinuousHostOptions(PairHostOptions):
    max_pairings: Optional[int] = None
    iteration_delay_secs: float = 1.0
    on_paired: Optional[Callable[[int, PairHostResult], None]] = None
    on_iteration_error: Optional[Callable[[BaseException, int], None]] = None


async def host_pairing_session_continuous(opts: ContinuousHostOptions) -> int:
    """Repeated host loop for App Store review demos. Same code stays
    valid across multiple joiners until max_pairings or abort_signal."""
    count = 0
    max_pairings = opts.max_pairings if opts.max_pairings is not None else 10**9
    while count < max_pairings:
        if opts.abort_signal is not None and opts.abort_signal.is_set():
            break
        try:
            result = await host_pairing_session(opts)
            count += 1
            if opts.on_paired is not None:
                try:
                    opts.on_paired(count, result)
                except Exception:
                    pass
        except BaseException as exc:
            if opts.abort_signal is not None and opts.abort_signal.is_set():
                break
            if opts.on_iteration_error is not None:
                try:
                    opts.on_iteration_error(exc, count)
                except Exception:
                    pass
        if count >= max_pairings:
            break
        if opts.iteration_delay_secs > 0:
            try:
                await asyncio.sleep(opts.iteration_delay_secs)
            except asyncio.CancelledError:
                break
    return count


# ─── Internals ────────────────────────────────────────────────────────────


def _parse_envelope(raw: object) -> Optional[dict]:
    try:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        return json.loads(raw)  # type: ignore[arg-type]
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return None


def _b64_decode_safe(s: str) -> bytes:
    import base64
    try:
        return base64.b64decode(s)
    except Exception:
        return b""
