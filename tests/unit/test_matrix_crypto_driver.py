"""CryptoDriver — the anti-UTD ordering + the outbound encrypt path.

These don't need the real binding: a fake OlmMachine with the documented method
surface (the .pyi contract) plus a fake gateway, sharing one event log so we can
assert cross-object ordering.
"""

from __future__ import annotations

import json

from chat4000_hermes_plugin.matrix.crypto_driver import CryptoDriver


class FakeMachine:
    def __init__(self, log: list):
        self.log = log
        self.outgoing: list[str] = []
        self.tracked: set[str] = set()
        self._pending_query: set[str] = set()

    def receive_sync_changes(self, td, dl, otk, fallback=None, nb=None):
        self.log.append("recv_sync_changes")
        self.last_nb = nb  # next_batch = the to-device cursor (atomic with keys)
        # Model matrix-sdk-crypto: a device_lists.changed entry for a TRACKED user
        # marks that user's device list dirty → a /keys/query is emitted on the next
        # drain. Only honored for already-tracked users (an untracked `changed` is
        # ignored), exactly as verified against the real binding.
        try:
            changed = (json.loads(dl) or {}).get("changed") or []
        except (json.JSONDecodeError, TypeError):
            changed = []
        for u in changed:
            if u in self.tracked:
                self._pending_query.add(u)
        return "[]"

    def outgoing_requests(self):
        self.log.append("outgoing_requests")
        out = list(self.outgoing)
        # Emit a /keys/query for any user marked dirty (drained once, then cleared —
        # the binding re-emits until mark_request_as_sent, but for the test one
        # drain suffices to assert the query was produced).
        for u in sorted(self._pending_query):
            out.append(
                json.dumps(
                    {
                        "id": f"q_{u}",
                        "kind": "keys_query",
                        "method": "POST",
                        "path": "/_matrix/client/v3/keys/query",
                        "body": {"device_keys": {u: []}},
                    }
                )
            )
        self._pending_query.clear()
        return out

    def mark_request_as_sent(self, rid, kind, status, body):
        self.log.append(("mark", rid, kind))

    def get_missing_sessions(self, users):
        return None

    def share_room_key(self, room_id, users):
        return []

    def encrypt_room_event(self, room_id, etype, content):
        self.log.append(("encrypt", room_id, etype))
        return json.dumps({"algorithm": "m.megolm.v1.aes-sha2", "ciphertext": "CT"})

    def decrypt_room_event(self, event, room_id):
        return json.dumps({"type": "m.room.message", "content": {"body": "hi"}})

    def update_tracked_users(self, users):
        # Idempotent: tracking an already-tracked user does NOT re-query (it just
        # stays tracked) — the exact behavior that makes a plain re-track insufficient
        # on redeem and forces the synthetic-changed path.
        self.tracked.update(users)


class FakeGateway:
    def __init__(self, log: list):
        self.log = log
        self.user_id = "@plugin:hs"
        self.requests: list = []

    async def ack_sync(self, pos, to_device_pos=None):
        self.log.append(("ack", pos, to_device_pos))

    async def request(self, method, path, body=None):
        self.log.append(("req", method, path))
        self.requests.append((method, path, body))
        return (200, {"event_id": "$evt"})


async def test_persist_before_ack():
    """The core anti-UTD invariant: receive_sync_changes (store write) MUST
    precede sync_ack (cursor advance)."""
    log: list = []
    d = CryptoDriver(FakeMachine(log), FakeGateway(log))
    await d.process_sync({"pos": "p1", "extensions": {"to_device": {"events": []}}})

    recv_i = log.index("recv_sync_changes")
    ack_i = next(i for i, e in enumerate(log) if isinstance(e, tuple) and e[0] == "ack")
    assert recv_i < ack_i, f"ack happened before persist: {log}"


async def test_no_ack_without_pos():
    log: list = []
    d = CryptoDriver(FakeMachine(log), FakeGateway(log))
    await d.process_sync({"extensions": {}})  # no pos
    assert not any(isinstance(e, tuple) and e[0] == "ack" for e in log)


async def test_to_device_cursor_persisted_atomically_and_acked():
    """The to-device cursor is written in the SAME store write as the keys (as the
    next_batch arg) and acked AFTER — never the cursor without its keys."""
    log: list = []
    m = FakeMachine(log)
    d = CryptoDriver(m, FakeGateway(log))
    await d.process_sync(
        {"pos": "p1", "to_device_pos": "td7", "extensions": {"to_device": {"events": []}}}
    )
    # persisted atomically with the keys (the receive_sync_changes next_batch):
    assert m.last_nb == "td7"
    # acked, carrying the to-device cursor, AFTER the store write:
    recv_i = log.index("recv_sync_changes")
    ack_i, ack = next((i, e) for i, e in enumerate(log) if isinstance(e, tuple) and e[0] == "ack")
    assert recv_i < ack_i
    assert ack == ("ack", "p1", "td7")


async def test_frame_with_no_to_device_passes_none_cursor():
    """A frame with no to-device section advances no to-device cursor (None → the
    gateway leaves it unchanged / carries forward)."""
    log: list = []
    m = FakeMachine(log)
    d = CryptoDriver(m, FakeGateway(log))
    await d.process_sync({"pos": "p2", "extensions": {}})
    assert m.last_nb is None
    assert ("ack", "p2", None) in log


async def test_send_room_event_splices_cleartext_envelope():
    """push + relates_to ride cleartext on the m.room.encrypted envelope, and the
    PUT goes to the m.room.encrypted send path."""
    log: list = []
    gw = FakeGateway(log)
    d = CryptoDriver(FakeMachine(log), gw)
    eid = await d.send_room_event(
        "!r:hs",
        "m.room.message",
        {"msgtype": "m.text", "body": "x"},
        ["@u:hs"],
        push=True,
        relates_to={"rel_type": "m.replace", "event_id": "$anchor"},
    )
    assert eid == "$evt"
    method, path, body = gw.requests[-1]
    assert method == "PUT"
    assert "/send/m.room.encrypted/" in path
    assert body["chat4000.push"] is True
    assert body["m.relates_to"] == {"rel_type": "m.replace", "event_id": "$anchor"}
    assert body["ciphertext"] == "CT"  # the encrypted content survived


async def test_decrypt_returns_cleartext():
    log: list = []
    d = CryptoDriver(FakeMachine(log), FakeGateway(log))
    clear = await d.decrypt({"type": "m.room.encrypted"}, "!r:hs")
    assert clear == {"type": "m.room.message", "content": {"body": "hi"}}


async def test_force_query_user_requeries_already_tracked_user():
    """Protocol E "Refresh the new device's keys on redeem": force_query_user MUST
    produce a /keys/query for the redeeming user EVEN THOUGH they are already tracked
    (from setup, when they had zero devices) — a plain re-track is idempotent and
    would NOT re-query, which is the proven UTD bug. The driver forces it via a
    synthetic device_lists.changed through receive_sync_changes + drain.
    """
    log: list = []
    m = FakeMachine(log)
    gw = FakeGateway(log)
    d = CryptoDriver(m, gw)

    user = "@u_target:hs"
    # Simulate the redeeming user being tracked from setup (zero devices then).
    m.tracked.add(user)
    # A plain re-track issues NO query (idempotent) — establish that baseline.
    await d.track_users([user])
    assert not any(method == "POST" and "keys/query" in path for method, path, _ in gw.requests), (
        "plain re-track must not re-query an already-tracked user"
    )

    # Now FORCE a re-query for the redeeming user.
    await d.force_query_user(user)

    queries = [
        body for method, path, body in gw.requests if "keys/query" in path and method == "POST"
    ]
    assert queries, "force_query_user produced no /keys/query"
    # The query targets the redeeming user specifically.
    assert any(user in (q or {}).get("device_keys", {}) for q in queries)

    # And it must NOT have advanced the to-device cursor (synthetic, next_batch=None).
    assert m.last_nb is None
