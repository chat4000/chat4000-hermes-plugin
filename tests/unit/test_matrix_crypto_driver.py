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

    def receive_sync_changes(self, td, dl, otk, fallback=None, nb=None):
        self.log.append("recv_sync_changes")
        return "[]"

    def outgoing_requests(self):
        self.log.append("outgoing_requests")
        return list(self.outgoing)

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
        pass


class FakeGateway:
    def __init__(self, log: list):
        self.log = log
        self.user_id = "@plugin:hs"
        self.requests: list = []

    async def ack_sync(self, pos):
        self.log.append(("ack", pos))

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
