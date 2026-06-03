"""Crypto driver — wires the gateway to the `chat4000_pyvodozemac` OlmMachine.

This is where the push/pull loop from the binding's `.pyi` contract actually
runs. It owns:
  - the **anti-UTD ordering**: `receive_sync_changes` (persists the store) →
    `sync_ack` → drain outgoing. Never ack before the store is written.
  - the outgoing-request drain (key upload/query/claim, to-device, signatures),
    each sent over the gateway and reported back with `mark_request_as_sent`,
    inside one critical section (the contract warns requests re-emit until marked).
  - the outbound encrypt path: establish Olm sessions → share the Megolm key →
    encrypt → PUT, with cleartext `m.relates_to`/`chat4000.push` spliced onto the
    `m.room.encrypted` envelope AFTER encryption.
  - the inbound decrypt path.

The binding's methods are synchronous (they `block_on` async crypto, releasing
the GIL), so we run them via `asyncio.to_thread` to keep the event loop free.

The `OlmMachine` is injected so this is unit-testable with a fake. The real one
comes from `chat4000_pyvodozemac` (a maturin wheel — see BUILD.md; not importable
until built).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Protocol

from .gateway_client import GatewayClient
from .sliding_sync import ParsedSync, parse_sync_frame

logger = logging.getLogger(__name__)


class OlmMachineLike(Protocol):
    """The surface we use from chat4000_pyvodozemac.OlmMachine (see its .pyi)."""

    def receive_sync_changes(
        self,
        to_device_events: str,
        changed_devices: str,
        one_time_key_counts: str,
        unused_fallback_keys: str | None = ...,
        next_batch: str | None = ...,
    ) -> str: ...
    def outgoing_requests(self) -> list[str]: ...
    def mark_request_as_sent(self, request_id: str, kind: str, status: int, body: str) -> None: ...
    def get_missing_sessions(self, user_ids: list[str]) -> str | None: ...
    def share_room_key(self, room_id: str, user_ids: list[str]) -> list[str]: ...
    def encrypt_room_event(self, room_id: str, event_type: str, content: str) -> str: ...
    def decrypt_room_event(self, event: str, room_id: str) -> str: ...
    def update_tracked_users(self, user_ids: list[str]) -> None: ...


def load_olm_machine(
    user_id: str, device_id: str, store_path: str, passphrase: str | None = None
) -> OlmMachineLike:
    """Construct the real binding. Imported lazily so this module loads (and the
    rest of the plugin runs) even before the maturin wheel is built."""
    from chat4000_pyvodozemac import OlmMachine

    machine: OlmMachineLike = OlmMachine(user_id, device_id, store_path, passphrase)
    return machine


class CryptoDriver:
    def __init__(self, machine: OlmMachineLike, gateway: GatewayClient) -> None:
        self._m = machine
        self._gw = gateway
        # One lock serializes all crypto-state mutations. Correctness over
        # concurrency: the binding warns that draining/marking and key-sharing
        # must not race.
        self._lock = asyncio.Lock()

    # ─── inbound ──────────────────────────────────────────────────────────

    async def process_sync(self, frame: dict[str, Any]) -> ParsedSync:
        """Ingest one `sync` frame. Persists room keys, THEN acks, THEN drains
        outgoing crypto work. Returns the parsed frame so the room layer can
        decrypt + dispatch the timeline."""
        parsed = parse_sync_frame(frame)

        # 1. Persist crypto state (room keys land in the store here). The to-device
        #    cursor is written as the store's next_batch in this SAME atomic write,
        #    so the cursor and the keys it acknowledges land together — never the
        #    cursor without its keys (which would let the homeserver delete keys we
        #    hadn't saved → permanent UTD). (protocol D, two cursors.)
        async with self._lock:
            decrypted_to_device = await asyncio.to_thread(
                self._m.receive_sync_changes,
                json.dumps(parsed.to_device_events),
                json.dumps(parsed.device_lists),
                json.dumps(parsed.one_time_key_counts),
                json.dumps(parsed.unused_fallback_keys)
                if parsed.unused_fallback_keys is not None
                else None,
                parsed.to_device_pos,
            )
        # Log room-key arrivals so we can match a UTD's missing session_id against
        # whether its key actually reached us (A1 in the UTD diagnostics).
        self._log_room_key_arrivals(decrypted_to_device)

        # 2. Store is durable → safe to advance BOTH upstream cursors. We ack the
        #    to-device cursor only AFTER its keys are persisted (anti-UTD); ack_sync
        #    carries the last to_device_pos forward on frames with no to-device.
        if parsed.pos:
            await self._gw.ack_sync(parsed.pos, parsed.to_device_pos)

        # 3. Push any crypto requests the machine now wants made.
        await self.drain_outgoing()
        return parsed

    async def decrypt(self, event: dict[str, Any], room_id: str) -> dict[str, Any] | None:
        """Decrypt one `m.room.encrypted` timeline event → cleartext event dict,
        or None on failure (logged, not raised — a single UTD shouldn't kill the
        sync loop)."""
        try:
            clear = await asyncio.to_thread(self._m.decrypt_room_event, json.dumps(event), room_id)
            decrypted: dict[str, Any] = json.loads(clear)
            return decrypted
        except Exception as exc:  # noqa: BLE001
            # A single UTD (unable-to-decrypt) must not kill the sync loop — report
            # once to the sink and return None so routing skips this event.
            from ..error_log import dump_chat4000_trace

            # Log the Megolm session_id + sender so we can correlate against the
            # SENDER's "I shared room_key session=X" log — same X => the key was
            # sent but we never got/decrypted it (our bug); X absent on their side
            # => they never shared it (client bug). See A1 (to_device arrivals).
            content = event.get("content") or {}
            logger.warning(
                "UTD decrypt failed: room=%s megolm_session_id=%s sender=%s "
                "sender_device=%s sender_key=%s: %s",
                room_id,
                content.get("session_id"),
                event.get("sender"),
                content.get("device_id"),
                content.get("sender_key"),
                exc,
            )
            dump_chat4000_trace(
                "matrix.decrypt",
                exc,
                {"room_id": room_id, "session_id": content.get("session_id")},
            )
            return None

    # ─── outbound ─────────────────────────────────────────────────────────

    async def send_room_event(
        self,
        room_id: str,
        event_type: str,
        content: dict[str, Any],
        members: list[str],
        *,
        push: bool | None = None,
        relates_to: dict[str, Any] | None = None,
        txn_id: str | None = None,
    ) -> str | None:
        """Encrypt `content` and send it to `room_id`. `push`/`relates_to` ride
        CLEARTEXT on the `m.room.encrypted` envelope (the homeserver reads them
        for push rules + relation aggregation). Returns the new event_id."""
        async with self._lock:
            # Establish sessions with any member device we lack, then share the
            # room key, then encrypt — all under the lock (binding requirement).
            claim = await asyncio.to_thread(self._m.get_missing_sessions, members)
            if claim:
                await self._send_and_mark(claim)
            for share in await asyncio.to_thread(self._m.share_room_key, room_id, members):
                await self._send_and_mark(share)
            enc_json = await asyncio.to_thread(
                self._m.encrypt_room_event, room_id, event_type, json.dumps(content)
            )

        enc: dict[str, Any] = json.loads(enc_json)
        # Splice cleartext envelope fields AFTER encryption.
        if relates_to is not None:
            enc["m.relates_to"] = relates_to
        if push is not None:
            enc["chat4000.push"] = push

        txn = txn_id or uuid.uuid4().hex[:32]
        status, body = await self._gw.request(
            "PUT",
            f"/_matrix/client/v3/rooms/{room_id}/send/m.room.encrypted/{txn}",
            enc,
        )
        if status >= 400:
            logger.warning("send to %s failed: %s %s", room_id, status, body)
            return None
        return body.get("event_id")

    async def track_users(self, user_ids: list[str]) -> None:
        async with self._lock:
            await asyncio.to_thread(self._m.update_tracked_users, user_ids)
        await self.drain_outgoing()

    # ─── internals ────────────────────────────────────────────────────────

    async def drain_outgoing(self) -> None:
        """Send every pending crypto request and mark it, in one critical
        section (the binding may re-emit a request until it's marked)."""
        async with self._lock:
            reqs = await asyncio.to_thread(self._m.outgoing_requests)
            for rjson in reqs:
                await self._send_and_mark(rjson)

    async def _send_and_mark(self, req_json: str) -> None:
        r = json.loads(req_json)
        kind = r.get("kind")
        # A3: prove the plugin publishes its one-time keys — without them no device
        # can build an Olm channel to us, which guarantees inbound UTD.
        if kind == "keys_upload":
            otk = (r.get("body") or {}).get("one_time_keys") or {}
            logger.info("keys_upload: %d one-time keys", len(otk) if isinstance(otk, dict) else 0)
        elif kind in ("keys_claim", "keys_query"):
            logger.info("crypto request: %s -> %s", kind, r.get("path"))
        status, body = await self._gw.request(r["method"], r["path"], r.get("body"))
        await asyncio.to_thread(
            self._m.mark_request_as_sent, r["id"], kind, status, json.dumps(body)
        )

    def _log_room_key_arrivals(self, decrypted_to_device_json: str) -> None:
        """A1: log incoming to-device events — especially m.room_key (the keys other
        devices share with us). Best-effort + defensive about the binding's shape."""
        try:
            events = json.loads(decrypted_to_device_json or "[]")
        except (json.JSONDecodeError, TypeError):
            return
        if not isinstance(events, list) or not events:
            return
        types = [
            str((e or {}).get("type") or (e or {}).get("event_type") or "?")
            for e in events
            if isinstance(e, dict)
        ]
        logger.info("to_device batch processed: %d events types=%s", len(events), types)
        for ev in events:
            if not isinstance(ev, dict) or "room_key" not in str(
                ev.get("type") or ev.get("event_type") or ""
            ):
                continue
            raw = ev.get("content")
            content = raw if isinstance(raw, dict) else ev
            logger.info(
                "  -> room_key megolm_session_id=%s room=%s sender_key=%s",
                content.get("session_id"),
                content.get("room_id"),
                content.get("sender_key"),
            )
