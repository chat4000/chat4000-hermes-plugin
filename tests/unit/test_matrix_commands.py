"""Control-room command handler — session.* + plugin.* (with fakes)."""

from __future__ import annotations

from chat4000_hermes_plugin.matrix.commands import CommandHandler


class FakeRooms:
    control_room_id = "!control:hs"

    def __init__(self):
        self.created: list = []
        self.invited: list = []
        self.renamed: list = []
        self.archived: list = []
        self.deleted: list = []

    async def create_session_room(self, title, agent_id):
        self.created.append((title, agent_id))
        return "!new:hs"

    async def invite_user(self, room, uid):
        self.invited.append((room, uid))

    async def create_session_room_and_invite(self, members, title="session", agent_id="main"):
        room_id = await self.create_session_room(title, agent_id)
        for uid in members:
            await self.invite_user(room_id, uid)
        return room_id

    async def rename_session(self, room, title):
        self.renamed.append((room, title))

    async def archive_session(self, room):
        self.archived.append(room)

    async def delete_session(self, room):
        self.deleted.append(room)


class FakeCrypto:
    def __init__(self):
        self.sent: list = []

    async def send_room_event(
        self, room, etype, content, members, *, push=None, relates_to=None, txn_id=None
    ):
        self.sent.append((room, content, push))
        return "$r"


class FakeSession:
    def __init__(self):
        self.rooms = FakeRooms()
        self.crypto = FakeCrypto()
        self.members = ["@u:hs"]

    def recipients(self, room_id):
        return list(self.members)


async def test_session_new_creates_invites_and_replies():
    s = FakeSession()
    await CommandHandler(s).handle(
        "!control:hs", "session.new", {"title": "deploy", "agent_id": "main"}
    )
    assert s.rooms.created == [("deploy", "main")]
    assert ("!new:hs", "@u:hs") in s.rooms.invited
    room, content, push = s.crypto.sent[-1]
    assert room == "!control:hs"
    assert content["msgtype"] == "chat4000.command_result"
    assert content["command"] == "session.new"
    assert content["ok"] is True and content["room_id"] == "!new:hs"
    assert push is False  # results never wake the user


async def test_session_rename_requires_args():
    s = FakeSession()
    await CommandHandler(s).handle("!control:hs", "session.rename", {"room_id": "!r:hs"})
    _, content, _ = s.crypto.sent[-1]
    assert content["ok"] is False


async def test_session_new_defaults_to_new_chat():
    s = FakeSession()
    await CommandHandler(s).handle("!control:hs", "session.new", {})
    assert s.rooms.created == [("New chat", "main")]


async def test_session_delete_leaves_forgets_and_replies():
    s = FakeSession()
    await CommandHandler(s).handle("!control:hs", "session.delete", {"room_id": "!old:hs"})
    assert s.rooms.deleted == ["!old:hs"]
    _, content, _ = s.crypto.sent[-1]
    assert content["command"] == "session.delete"
    assert content["ok"] is True
    assert content["room_id"] == "!old:hs"


async def test_plugin_update_is_refused():
    s = FakeSession()
    await CommandHandler(s).handle("!control:hs", "plugin.update", {"version": "9.9.9"})
    _, content, _ = s.crypto.sent[-1]
    assert content["command"] == "plugin.update" and content["ok"] is False


async def test_update_check_is_readonly():
    s = FakeSession()
    await CommandHandler(s, version="2.1.0").handle("!control:hs", "plugin.update_check", {})
    _, content, _ = s.crypto.sent[-1]
    assert content["ok"] is True
    assert content["current_version"] == "2.1.0"
    assert content["updatable"] is False


async def test_unknown_command_replies_not_ok():
    s = FakeSession()
    await CommandHandler(s).handle("!control:hs", "bogus.cmd", {})
    _, content, _ = s.crypto.sent[-1]
    assert content["ok"] is False
