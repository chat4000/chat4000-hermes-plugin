"""Host auto-title pickup (post_llm_call → read-only state.db poll).

The hook starts ONE poller per session on the adapter loop; the poller reads
hermes_state.SessionDB(read_only=True).get_session_title() until the host's
daemon-thread titler stores a title, then applies it via the adapter's
_apply_host_session_title. Interval/budget are module constants, patched to ~0
here so no test sleeps for real. Module-global state is cleared around tests.
"""

from __future__ import annotations

import asyncio
import sys

import chat4000_hermes_plugin.plugin_hooks as h
from chat4000_hermes_plugin.matrix.rooms import derive_first_message_title

_HISTORY_FIRST_EXCHANGE = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": "hi"},
    {"role": "assistant", "content": "yo"},
]


class FakeAdapter:
    def __init__(self, loop, room="!room"):
        self._connected = True
        self._loop = loop
        self._room = room
        self.applied: list[tuple[str, str]] = []

    def _room_for_session(self, session_id):
        return self._room

    async def _apply_host_session_title(self, room_id, title):
        self.applied.append((room_id, title))


def _fake_db_cls(titles):
    """A stand-in reader (the _open_title_reader seam): each get_session_title()
    pops the next entry of `titles` (then keeps returning None)."""

    class _DB:
        instances: list = []

        def __init__(self):
            self._seq = list(titles)
            self.closed = False
            _DB.instances.append(self)

        def get_session_title(self, session_id):
            return self._seq.pop(0) if self._seq else None

        def close(self):
            self.closed = True

    return _DB


def _clear():
    h._SESSION_PLATFORM.clear()
    h._TITLE_POLLS_ACTIVE.clear()
    h._TITLE_APPLIED_ROOMS.clear()


async def _drain():
    for _ in range(6):
        await asyncio.sleep(0)


def _patch_timing(monkeypatch, interval=0.0, budget=1.0):
    monkeypatch.setattr(h, "_TITLE_POLL_INTERVAL_S", interval)
    monkeypatch.setattr(h, "_TITLE_POLL_BUDGET_S", budget)


async def test_title_applied_on_second_poll(monkeypatch):
    a = FakeAdapter(asyncio.get_running_loop())
    _clear()
    _patch_timing(monkeypatch)
    db_cls = _fake_db_cls([None, "Fix login flow"])
    monkeypatch.setattr(h, "_open_title_reader", db_cls)
    try:
        await h._poll_host_title(a, "s1", "!room")
        assert a.applied == [("!room", "Fix login flow")]  # once, right room+title
        assert "!room" in h._TITLE_APPLIED_ROOMS  # success blocks re-polls
        assert db_cls.instances[0].closed  # ro connection released
    finally:
        _clear()


async def test_timeout_gives_up_silently(monkeypatch):
    a = FakeAdapter(asyncio.get_running_loop())
    _clear()
    _patch_timing(monkeypatch, budget=0.0)  # deadline passes after the 1st read
    db_cls = _fake_db_cls([])  # host never titles the session
    monkeypatch.setattr(h, "_open_title_reader", db_cls)
    try:
        await h._poll_host_title(a, "s1", "!room")  # must not raise
        assert a.applied == []
        assert "!room" not in h._TITLE_APPLIED_ROOMS
        assert db_cls.instances[0].closed
    finally:
        _clear()


async def test_missing_hermes_state_is_silent_noop(monkeypatch):
    """Unit tests/CI run outside the Hermes host: importing hermes_state raises
    ImportError and the poller gives up without touching the adapter."""
    monkeypatch.setitem(sys.modules, "hermes_state", None)  # forces ImportError
    a = FakeAdapter(asyncio.get_running_loop())
    _clear()
    try:
        await h._poll_host_title(a, "s1", "!room")  # must not raise
        assert a.applied == []
    finally:
        _clear()


def test_reader_prefers_sessiondb_read_only_mode(monkeypatch):
    """Hermes >=0.15: SessionDB(read_only=True) is used directly."""
    import types

    fake = types.ModuleType("hermes_state")

    class _RoDB:
        def __init__(self, read_only=False):
            assert read_only is True  # plugin must NEVER open state.db writable
            self.ro = read_only

    fake.SessionDB = _RoDB
    fake.DEFAULT_DB_PATH = "/nonexistent"
    monkeypatch.setitem(sys.modules, "hermes_state", fake)
    reader = h._open_title_reader()
    assert isinstance(reader, _RoDB) and reader.ro is True


def test_reader_falls_back_to_raw_ro_sqlite_on_old_hosts(monkeypatch, tmp_path):
    """Hermes <=0.14: SessionDB has no read_only kwarg (TypeError — the
    hermes-test-87 crash). The reader must drop to a raw mode=ro sqlite URI
    connection that can read titles but can never write."""
    import sqlite3
    import types

    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT)")
    conn.execute("INSERT INTO sessions VALUES ('s1', 'Top CNBC News Headlines')")
    conn.commit()
    conn.close()

    class _OldDB:
        # 0.14 signature: no read_only kwarg → calling with read_only=True
        # raises TypeError before __init__ runs; the body guards against any
        # writable construction attempt.
        def __init__(self, db_path=None):
            raise AssertionError("plugin must not construct a writable SessionDB")

    fake = types.ModuleType("hermes_state")
    fake.SessionDB = _OldDB
    fake.DEFAULT_DB_PATH = db_path
    monkeypatch.setitem(sys.modules, "hermes_state", fake)

    reader = h._open_title_reader()
    assert isinstance(reader, h._RawTitleReader)
    assert reader.get_session_title("s1") == "Top CNBC News Headlines"
    assert reader.get_session_title("missing") is None
    import pytest

    with pytest.raises(sqlite3.OperationalError):  # mode=ro: writes are impossible
        reader._conn.execute("INSERT INTO sessions VALUES ('x', 'y')")
    reader.close()


async def test_dedupe_two_hook_fires_one_poller(monkeypatch):
    a = FakeAdapter(asyncio.get_running_loop())
    h.register_active_adapter(a)
    _clear()
    started: list[str] = []

    async def fake_poll(adapter, session_id, room):
        started.append(session_id)

    monkeypatch.setattr(h, "_poll_host_title", fake_poll)
    try:
        h.on_post_llm_call(
            session_id="s1", conversation_history=_HISTORY_FIRST_EXCHANGE, platform="chat4000"
        )
        h.on_post_llm_call(
            session_id="s1", conversation_history=_HISTORY_FIRST_EXCHANGE, platform="chat4000"
        )
        await _drain()
        assert started == ["s1"]
    finally:
        h.deregister_active_adapter(a)
        _clear()


async def test_contextvar_room_wins_when_session_map_misses(monkeypatch):
    """Hermes <=0.14 passes the RUNTIME session id to post_llm_call, which never
    matches the session map's build_session_key keys — the room must then come
    from the task-local chat contextvar (the hermes-test-86 bug)."""
    a = FakeAdapter(asyncio.get_running_loop(), room=None)  # map miss
    h.register_active_adapter(a)
    _clear()
    started: list[tuple[str, str]] = []

    async def fake_poll(adapter, session_id, room):
        started.append((session_id, room))

    monkeypatch.setattr(h, "_poll_host_title", fake_poll)
    monkeypatch.setattr(h, "_current_chat_id", lambda: "!ctxroom")
    try:
        h.on_post_llm_call(
            session_id="20260612_065320_0a14a1aa",
            conversation_history=_HISTORY_FIRST_EXCHANGE,
            platform="chat4000",
        )
        await _drain()
        assert started == [("20260612_065320_0a14a1aa", "!ctxroom")]
    finally:
        h.deregister_active_adapter(a)
        _clear()


async def test_already_titled_room_is_not_repolled(monkeypatch):
    a = FakeAdapter(asyncio.get_running_loop())
    h.register_active_adapter(a)
    _clear()
    started: list[str] = []

    async def fake_poll(adapter, session_id, room):
        started.append(session_id)

    monkeypatch.setattr(h, "_poll_host_title", fake_poll)
    h._TITLE_APPLIED_ROOMS.add(a._room)  # an earlier poll already succeeded
    try:
        h.on_post_llm_call(
            session_id="s1", conversation_history=_HISTORY_FIRST_EXCHANGE, platform="chat4000"
        )
        await _drain()
        assert started == []
    finally:
        h.deregister_active_adapter(a)
        _clear()


async def test_non_chat4000_platform_is_ignored(monkeypatch):
    a = FakeAdapter(asyncio.get_running_loop())
    h.register_active_adapter(a)
    _clear()  # session NOT registered as chat4000 either
    started: list[str] = []

    async def fake_poll(adapter, session_id, room):
        started.append(session_id)

    monkeypatch.setattr(h, "_poll_host_title", fake_poll)
    try:
        h.on_post_llm_call(
            session_id="s9", conversation_history=_HISTORY_FIRST_EXCHANGE, platform="telegram"
        )
        await _drain()
        assert started == []
    finally:
        h.deregister_active_adapter(a)
        _clear()


async def test_later_exchanges_do_not_start_a_poller(monkeypatch):
    """Mirror of the host titler heuristic: > 2 user messages → no title coming."""
    a = FakeAdapter(asyncio.get_running_loop())
    h.register_active_adapter(a)
    _clear()
    started: list[str] = []

    async def fake_poll(adapter, session_id, room):
        started.append(session_id)

    monkeypatch.setattr(h, "_poll_host_title", fake_poll)
    history = [{"role": "user", "content": f"m{i}"} for i in range(3)]
    try:
        h.on_post_llm_call(session_id="s1", conversation_history=history, platform="chat4000")
        await _drain()
        assert started == []
    finally:
        h.deregister_active_adapter(a)
        _clear()


def test_command_first_message_never_becomes_title():
    assert derive_first_message_title("/approve") is None
    assert derive_first_message_title("  /approve please  ") is None
    assert derive_first_message_title("Fix login. It crashes later") == "Fix login"
