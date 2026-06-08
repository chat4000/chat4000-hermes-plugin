"""RoomManager protocol behavior: naming and local delete."""

from __future__ import annotations

from typing import Any

from chat4000_hermes_plugin.matrix.rooms import (
    DEFAULT_SESSION_ROOM_NAME,
    RoomManager,
    derive_first_message_title,
)


class FakeGateway:
    user_id = "@plugin:hs"

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, Any] | None]] = []
        self._next_room = 0

    async def request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> tuple[int, dict[str, Any]]:
        self.requests.append((method, path, body))
        if method == "POST" and path == "/_matrix/client/v3/createRoom":
            self._next_room += 1
            return 200, {"room_id": f"!room{self._next_room}:hs"}
        if method == "GET" and path.endswith("/state/m.room.name/"):
            return 200, {"name": DEFAULT_SESSION_ROOM_NAME}
        return 200, {}


async def test_create_session_room_defaults_to_new_chat() -> None:
    gw = FakeGateway()
    rooms = RoomManager(gw, "hs")
    room_id = await rooms.create_session_room("")
    assert room_id == "!room1:hs"
    assert gw.requests[0][2]["name"] == DEFAULT_SESSION_ROOM_NAME


def test_derive_first_message_title() -> None:
    assert derive_first_message_title("  Fix   login. It crashes later ") == "Fix login"
    assert derive_first_message_title("x" * 80) == "x" * 50
    assert derive_first_message_title("   ") is None


async def test_first_message_title_only_renames_new_chat() -> None:
    gw = FakeGateway()
    rooms = RoomManager(gw, "hs")
    rooms._room_names["!r:hs"] = DEFAULT_SESSION_ROOM_NAME
    await rooms.maybe_set_first_message_title("!r:hs", "Fix login. It crashes")
    assert (
        "PUT",
        "/_matrix/client/v3/rooms/!r:hs/state/m.room.name/",
        {"name": "Fix login"},
    ) in gw.requests

    gw.requests.clear()
    rooms._room_names["!r:hs"] = "Manual name"
    await rooms.maybe_set_first_message_title("!r:hs", "Should not apply")
    assert gw.requests == []


async def test_host_title_can_replace_auto_title_not_manual_title() -> None:
    gw = FakeGateway()
    rooms = RoomManager(gw, "hs")
    rooms._room_names["!r:hs"] = DEFAULT_SESSION_ROOM_NAME
    await rooms.maybe_set_first_message_title("!r:hs", "Fix login. It crashes")
    await rooms.maybe_apply_host_title("!r:hs", "AI login title")
    assert gw.requests[-1] == (
        "PUT",
        "/_matrix/client/v3/rooms/!r:hs/state/m.room.name/",
        {"name": "AI login title"},
    )

    gw.requests.clear()
    rooms._room_names["!r:hs"] = "Manual name"
    await rooms.maybe_apply_host_title("!r:hs", "Ignored AI title")
    assert gw.requests == []


async def test_delete_session_unlinks_leaves_and_forgets() -> None:
    gw = FakeGateway()
    rooms = RoomManager(gw, "hs", space_id="!space:hs")
    await rooms.delete_session("!old:hs")
    assert gw.requests == [
        ("PUT", "/_matrix/client/v3/rooms/!space:hs/state/m.space.child/!old:hs", {}),
        ("POST", "/_matrix/client/v3/rooms/!old:hs/leave", {}),
        ("POST", "/_matrix/client/v3/rooms/!old:hs/forget", {}),
    ]
