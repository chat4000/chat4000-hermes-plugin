"""MatrixSession routing — the command boundary (E) and message dispatch.

These exercise `_handle_encrypted` with injected fakes (no binding, no socket):
commands are honored ONLY in the control room; user messages route from session
rooms; own echoes are dropped.
"""

from __future__ import annotations

from chat4000_hermes_plugin.matrix.creds_store import BotCreds
from chat4000_hermes_plugin.matrix.session import MatrixSession


class FakeGateway:
    user_id = "@plugin:hs"


class FakeCrypto:
    def __init__(self, clear):
        self._clear = clear

    async def decrypt(self, ev, room_id):
        return self._clear


class FakeRooms:
    control_room_id = "!control:hs"

    def classify_room(self, *a):
        return None


def _session(clear, captured):
    creds = BotCreds("@plugin:hs", "DEV", "tok", "wss://gw/ws")
    async def on_msg(r, s, c, eid=""):
        captured.append(("msg", r, s, c))
    async def on_cmd(r, cmd, c):
        captured.append(("cmd", r, cmd, c))
    s = MatrixSession(creds, on_user_message=on_msg, on_command=on_cmd)
    s.gateway = FakeGateway()
    s.crypto = FakeCrypto(clear)
    s.rooms = FakeRooms()
    return s


ENC = {"type": "m.room.encrypted", "sender": "@u:hs"}


async def test_command_in_control_room_is_honored():
    cap: list = []
    clear = {"type": "m.room.message",
             "content": {"msgtype": "chat4000.command", "command": "session.new", "title": "x"}}
    await _session(clear, cap)._handle_encrypted("!control:hs", ENC)
    assert cap == [("cmd", "!control:hs", "session.new", clear["content"])]


async def test_command_outside_control_room_is_ignored():
    cap: list = []
    clear = {"type": "m.room.message",
             "content": {"msgtype": "chat4000.command", "command": "plugin.update"}}
    await _session(clear, cap)._handle_encrypted("!session:hs", ENC)
    assert cap == []  # command boundary: a session-room command does nothing


async def test_user_message_in_session_room_routes():
    cap: list = []
    clear = {"type": "m.room.message", "content": {"msgtype": "m.text", "body": "hi"}}
    await _session(clear, cap)._handle_encrypted("!session:hs", ENC)
    assert cap == [("msg", "!session:hs", "@u:hs", clear["content"])]


async def test_own_echo_is_dropped():
    cap: list = []
    clear = {"type": "m.room.message", "content": {"msgtype": "m.text", "body": "hi"}}
    own = {"type": "m.room.encrypted", "sender": "@plugin:hs"}
    await _session(clear, cap)._handle_encrypted("!session:hs", own)
    assert cap == []


# ─── membership-driven key sharing + read receipts ─────────────────────────

class TrackingCrypto:
    """FakeCrypto that records track_users + decrypt passthrough."""

    def __init__(self, clear=None):
        self._clear = clear
        self.tracked: list = []

    async def track_users(self, user_ids):
        self.tracked.append(list(user_ids))

    async def decrypt(self, ev, room_id):
        return self._clear

    async def process_sync(self, frame):
        from chat4000_hermes_plugin.matrix.sliding_sync import parse_sync_frame

        return parse_sync_frame(frame)


class RequestGateway:
    user_id = "@plugin:hs"

    def __init__(self):
        self.requests: list = []

    async def request(self, method, path, body=None):
        self.requests.append((method, path, body))
        return 200, {}


def _bare_session():
    creds = BotCreds("@plugin:hs", "DEV", "tok", "wss://gw/ws")
    s = MatrixSession(creds)
    s.gateway = RequestGateway()
    s.crypto = TrackingCrypto()
    s.rooms = FakeRooms()
    return s


async def test_recipients_union_paired_and_joined_minus_bot():
    s = _bare_session()
    await s.set_members(["@owner:hs"])
    # A second user joins the room (e.g. a device/user that wasn't known at connect).
    await s._update_room_membership("!sess:hs", {
        "required_state": [
            {"type": "m.room.member", "state_key": "@joiner:hs", "content": {"membership": "join"}},
            {"type": "m.room.member", "state_key": "@plugin:hs", "content": {"membership": "join"}},
        ],
    })
    # The bot is never a recipient; paired owner + the live joiner both are.
    assert s.recipients("!sess:hs") == ["@joiner:hs", "@owner:hs"]
    # A different room only sees the paired floor.
    assert s.recipients("!other:hs") == ["@owner:hs"]


async def test_membership_change_retracks_devices():
    s = _bare_session()
    await s.set_members(["@owner:hs"])          # track #1: {owner}
    await s._update_room_membership("!sess:hs", {
        "timeline": [
            {"type": "m.room.member", "state_key": "@joiner:hs", "content": {"membership": "join"}},
        ],
    })                                           # track #2: {owner, joiner}
    # Same membership again → no redundant /keys/query.
    await s._update_room_membership("!sess:hs", {
        "timeline": [
            {"type": "m.room.member", "state_key": "@joiner:hs", "content": {"membership": "join"}},
        ],
    })
    assert s.crypto.tracked == [["@owner:hs"], ["@joiner:hs", "@owner:hs"]]


async def test_leave_drops_recipient_and_retracks():
    s = _bare_session()
    await s.set_members([])
    await s._update_room_membership("!sess:hs", {
        "timeline": [{"type": "m.room.member", "state_key": "@u:hs", "content": {"membership": "join"}}],
    })
    assert s.recipients("!sess:hs") == ["@u:hs"]
    await s._update_room_membership("!sess:hs", {
        "timeline": [{"type": "m.room.member", "state_key": "@u:hs", "content": {"membership": "leave"}}],
    })
    assert s.recipients("!sess:hs") == []
    assert s.crypto.tracked == [["@u:hs"], []]


async def test_user_message_sends_read_receipt():
    creds = BotCreds("@plugin:hs", "DEV", "tok", "wss://gw/ws")
    cap: list = []
    async def on_msg(r, sender, c, eid=""):
        cap.append((r, sender, c, eid))
    s = MatrixSession(creds, on_user_message=on_msg)
    s.gateway = RequestGateway()
    s.crypto = TrackingCrypto({"type": "m.room.message", "content": {"msgtype": "m.text", "body": "hi"}})
    s.rooms = FakeRooms()
    ev = {"type": "m.room.encrypted", "sender": "@u:hs", "event_id": "$msg1"}
    await s._handle_encrypted("!sess:hs", ev)
    # Public read receipt POSTed for the exact inbound event, then routed to Hermes
    # with the question event_id (for chat4000.status references).
    assert ("POST", "/_matrix/client/v3/rooms/!sess:hs/receipt/m.read/$msg1", {}) in s.gateway.requests
    assert cap == [("!sess:hs", "@u:hs", {"msgtype": "m.text", "body": "hi"}, "$msg1")]


async def test_wait_first_sync_blocks_until_first_sync() -> None:
    creds = BotCreds("@plugin:hs", "DEV", "tok", "wss://gw/ws")
    s = MatrixSession(creds)
    s.gateway = RequestGateway()
    s.crypto = TrackingCrypto()
    s.rooms = FakeRooms()
    # Not synced yet → times out (wizard proceeds anyway, but marker isn't written).
    assert await s.wait_first_sync(timeout=0.05) is False
    # A processed sync flips it → ready.
    await s._on_sync({"pos": "p1"})
    assert await s.wait_first_sync(timeout=0.05) is True
