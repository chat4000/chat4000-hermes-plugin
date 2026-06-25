"""Auto-create-at-pairing: ONE initial session room per paired user, durably deduped.

A freshly-paired user gets one session room auto-created + an invite so their first
chat works without pressing "New Session". The onboarded store (user→room) is the
DURABLE dedupe: a restart re-reads known-users and re-invites everyone, but must NOT
mint a second initial room. The adapter is Hermes-host-coupled, so we build a bare
instance via __new__ and drive `_ensure_initial_session` with a fake RoomManager.
"""

from __future__ import annotations

import chat4000_hermes_plugin.matrix.users_store as us
from chat4000_hermes_plugin.matrix.hermes_adapter import Chat4000MatrixAdapter


class _FakeRooms:
    def __init__(self) -> None:
        self.created: list[tuple[list[str], str]] = []
        self._n = 0

    async def create_session_room_and_invite(self, members, title="session", agent_id="main"):  # type: ignore[no-untyped-def]
        self._n += 1
        room_id = f"!sess{self._n}:hs"
        self.created.append((list(members), room_id))
        return room_id


class _FakeSession:
    def __init__(self, rooms: _FakeRooms) -> None:
        self.rooms = rooms


class _StoppableSession:
    def __init__(self) -> None:
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


def _adapter(rooms: _FakeRooms, account_id: str = "default") -> Chat4000MatrixAdapter:
    a = Chat4000MatrixAdapter.__new__(Chat4000MatrixAdapter)
    a._session = _FakeSession(rooms)  # type: ignore[assignment]
    a._account_id = account_id
    return a


async def test_fresh_user_gets_one_room_and_is_marked(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(us, "resolve_chat4000_plugin_dir", lambda: tmp_path)
    rooms = _FakeRooms()
    await _adapter(rooms)._ensure_initial_session("@u:hs")
    # Exactly one room created, that user invited, durably recorded user→room.
    assert rooms.created == [(["@u:hs"], "!sess1:hs")]
    assert us.load_onboarded() == {"@u:hs": "!sess1:hs"}


async def test_already_onboarded_user_gets_no_second_room(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(us, "resolve_chat4000_plugin_dir", lambda: tmp_path)
    us.mark_onboarded("@u:hs", "!existing:hs")  # a prior run already onboarded them
    rooms = _FakeRooms()
    await _adapter(rooms)._ensure_initial_session("@u:hs")
    # Durable dedupe after a restart: NO new room, the mapping is untouched.
    assert rooms.created == []
    assert us.load_onboarded() == {"@u:hs": "!existing:hs"}


async def test_repeated_calls_are_idempotent(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(us, "resolve_chat4000_plugin_dir", lambda: tmp_path)
    rooms = _FakeRooms()
    a = _adapter(rooms)
    await a._ensure_initial_session("@u:hs")  # e.g. connect()
    await a._ensure_initial_session("@u:hs")  # e.g. a later invite-watch poll
    assert len(rooms.created) == 1  # never a second room


async def test_failed_connect_cleanup_stops_partial_session() -> None:
    session = _StoppableSession()
    a = Chat4000MatrixAdapter.__new__(Chat4000MatrixAdapter)
    a._connected = True
    a._commands = object()
    a._media = object()
    a._pair_listener = object()
    a._version_poller = object()
    a._invited = {"@u:hs"}
    a._session = session
    a._clear_ready = lambda: None  # type: ignore[method-assign]

    await a._cleanup_failed_connect()

    assert session.stopped is True
    assert a._session is None
    assert a._connected is False
    assert a._commands is None
    assert a._media is None
    assert a._pair_listener is None
    assert a._version_poller is None
    assert a._invited == set()
