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
    async def on_msg(r, s, c):
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
