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


def load_olm_machine(user_id: str, device_id: str, store_path: str, passphrase: str | None = None):
    """Construct the real binding. Imported lazily so this module loads (and the
    rest of the plugin runs) even before the maturin wheel is built."""
    from chat4000_pyvodozemac import OlmMachine  # type: ignore[import-not-found]

    return OlmMachine(user_id, device_id, store_path, passphrase)


class CryptoDriver:
    def __init__(self, machine: OlmMachineLike, gateway: GatewayClient):
        self._m = machine
        self._gw = gateway
        # One lock serializes all crypto-state mutations. Correctness over
        # concurrency: the binding warns that draining/marking and key-sharing
        # must not race.
        self._lock = asyncio.Lock()

    # ─── inbound ──────────────────────────────────────────────────────────

    async def process_sync(self, frame: dict) -> ParsedSync:
        """Ingest one `sync` frame. Persists room keys, THEN acks, THEN drains
        outgoing crypto work. Returns the parsed frame so the room layer can
        decrypt + dispatch the timeline."""
        parsed = parse_sync_frame(frame)

        # 1. Persist crypto state (room keys land in the store here).
        async with self._lock:
            await asyncio.to_thread(
                self._m.receive_sync_changes,
                json.dumps(parsed.to_device_events),
                json.dumps(parsed.device_lists),
                json.dumps(parsed.one_time_key_counts),
                json.dumps(parsed.unused_fallback_keys)
                if parsed.unused_fallback_keys is not None
                else None,
                parsed.pos,
            )

        # 2. Store is durable → safe to advance the gateway cursor.
        if parsed.pos:
            await self._gw.ack_sync(parsed.pos)

        # 3. Push any crypto requests the machine now wants made.
        await self.drain_outgoing()
        return parsed

    async def decrypt(self, event: dict, room_id: str) -> dict | None:
        """Decrypt one `m.room.encrypted` timeline event → cleartext event dict,
        or None on failure (logged, not raised — a single UTD shouldn't kill the
        sync loop)."""
        try:
            clear = await asyncio.to_thread(self._m.decrypt_room_event, json.dumps(event), room_id)
            return json.loads(clear)
        except Exception as exc:  # noqa: BLE001
            logger.warning("decrypt failed in %s: %s", room_id, exc)
            return None

    # ─── outbound ─────────────────────────────────────────────────────────

    async def send_room_event(
        self,
        room_id: str,
        event_type: str,
        content: dict,
        members: list[str],
        *,
        push: bool | None = None,
        relates_to: dict | None = None,
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
        status, body = await self._gw.request(r["method"], r["path"], r.get("body"))
        await asyncio.to_thread(
            self._m.mark_request_as_sent, r["id"], r["kind"], status, json.dumps(body)
        )
