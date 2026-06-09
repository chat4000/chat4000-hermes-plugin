"""Control-room command handler — session.* + plugin.* (with fakes)."""

from __future__ import annotations

import asyncio

from chat4000_hermes_plugin.matrix import commands as matrix_commands
from chat4000_hermes_plugin.matrix.commands import CommandHandler
from chat4000_hermes_plugin.matrix.registrar_client import RegistrarError


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
    def __init__(self, plugin_id="plugin-00000000-0000-0000-0000-000000000000"):
        self.rooms = FakeRooms()
        self.crypto = FakeCrypto()
        self.members = ["@u:hs"]
        self.plugin_id = plugin_id

    def recipients(self, room_id):
        return list(self.members)


class FakeRegistrar:
    def __init__(self, statuses=None, register_error=None):
        self.register_calls: list = []
        self.status_calls: list = []
        self.statuses = list(statuses or [])
        self.register_error = register_error

    async def register(
        self,
        code,
        *,
        kind="user",
        plugin_id=None,
        user_id=None,
        ttl_seconds=None,
    ):
        self.register_calls.append(
            {
                "code": code,
                "kind": kind,
                "plugin_id": plugin_id,
                "user_id": user_id,
                "ttl_seconds": ttl_seconds,
            }
        )
        if self.register_error is not None:
            raise self.register_error
        return {"ok": True, "expires_at": 123}

    async def status(self, code):
        self.status_calls.append(code)
        if self.statuses:
            return self.statuses.pop(0)
        return {"status": "pending"}


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


async def test_device_pair_start_registers_sender_bound_code_and_replies(monkeypatch):
    monkeypatch.setattr(matrix_commands, "_gen_device_pair_code", lambda: "428913")
    monkeypatch.setattr(matrix_commands, "_gen_pair_id", lambda: "p_7af3c1")
    s = FakeSession()
    reg = FakeRegistrar()
    handler = CommandHandler(s, registrar=reg)

    await handler.handle(
        "!control:hs",
        "device.pair_start",
        {"user_id": "@body-must-not-bind:hs"},
        sender="@sender:hs",
    )

    assert reg.register_calls == [
        {
            "code": "428913",
            "kind": "user",
            "plugin_id": s.plugin_id,
            "user_id": "@sender:hs",
            "ttl_seconds": 120,
        }
    ]
    room, content, push = s.crypto.sent[-1]
    assert room == "!control:hs"
    assert push is False
    assert content == {
        "msgtype": "chat4000.command_result",
        "command": "device.pair_start",
        "pair_id": "p_7af3c1",
        "code": "428913",
    }

    await handler.handle("!control:hs", "device.pair_cancel", {"pair_id": "p_7af3c1"})


async def test_device_pair_completed_emits_pair_status_without_invites(monkeypatch):
    monkeypatch.setattr(matrix_commands, "_gen_device_pair_code", lambda: "428913")
    monkeypatch.setattr(matrix_commands, "_gen_pair_id", lambda: "p_7af3c1")
    s = FakeSession()
    reg = FakeRegistrar(statuses=[{"status": "completed", "user_id": "@sender:hs"}])

    await CommandHandler(s, registrar=reg).handle(
        "!control:hs", "device.pair_start", {}, sender="@sender:hs"
    )
    await asyncio.sleep(0)

    assert s.rooms.invited == []
    assert s.crypto.sent[-1] == (
        "!control:hs",
        {"msgtype": "chat4000.pair_status", "pair_id": "p_7af3c1", "state": "completed"},
        False,
    )


async def test_device_pair_cancel_emits_result_and_cancelled_status(monkeypatch):
    monkeypatch.setattr(matrix_commands, "_gen_device_pair_code", lambda: "428913")
    monkeypatch.setattr(matrix_commands, "_gen_pair_id", lambda: "p_7af3c1")
    s = FakeSession()
    handler = CommandHandler(s, registrar=FakeRegistrar())

    await handler.handle("!control:hs", "device.pair_start", {}, sender="@sender:hs")
    await handler.handle("!control:hs", "device.pair_cancel", {"pair_id": "p_7af3c1"})

    assert s.crypto.sent[-2:] == [
        (
            "!control:hs",
            {
                "msgtype": "chat4000.command_result",
                "command": "device.pair_cancel",
                "pair_id": "p_7af3c1",
            },
            False,
        ),
        (
            "!control:hs",
            {"msgtype": "chat4000.pair_status", "pair_id": "p_7af3c1", "state": "cancelled"},
            False,
        ),
    ]


async def test_device_pair_start_failure_replies_and_emits_error_status(monkeypatch):
    monkeypatch.setattr(matrix_commands, "_gen_device_pair_code", lambda: "428913")
    monkeypatch.setattr(matrix_commands, "_gen_pair_id", lambda: "p_7af3c1")
    s = FakeSession()
    reg = FakeRegistrar(register_error=RegistrarError(503, "M_UNAVAILABLE", "offline"))

    await CommandHandler(s, registrar=reg).handle(
        "!control:hs", "device.pair_start", {}, sender="@sender:hs"
    )

    assert s.crypto.sent == [
        (
            "!control:hs",
            {
                "msgtype": "chat4000.command_result",
                "command": "device.pair_start",
                "pair_id": "p_7af3c1",
                "error": "503 M_UNAVAILABLE: offline",
            },
            False,
        ),
        (
            "!control:hs",
            {
                "msgtype": "chat4000.pair_status",
                "pair_id": "p_7af3c1",
                "state": "error",
                "error": "503 M_UNAVAILABLE: offline",
            },
            False,
        ),
    ]


async def test_unknown_command_replies_not_ok():
    s = FakeSession()
    await CommandHandler(s).handle("!control:hs", "bogus.cmd", {})
    _, content, _ = s.crypto.sent[-1]
    assert content["ok"] is False
