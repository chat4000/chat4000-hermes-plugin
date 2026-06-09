"""chat4000.status lifecycle (protocol e3d9358).

Live activity is a fresh E2EE chat4000.status timeline event per transition,
referencing the QUESTION event_id, re-sent every 4s as a keep-alive, and ALWAYS
closed with one idle. No native typing.

The adapter is Hermes-host-coupled, so we build a bare instance via __new__ and
drive the pure status methods with a fake turn writer.
"""

from __future__ import annotations

import asyncio
from typing import Any

import chat4000_hermes_plugin.matrix.hermes_adapter as ha
from chat4000_hermes_plugin.matrix.hermes_adapter import Chat4000MatrixAdapter

Record = tuple[Any, ...]


class _FakeTurnWriter:
    def __init__(self, sink: list[Record]) -> None:
        self._sink = sink

    async def send_status(self, room_id: str, state: str, question_event_id: str) -> None:
        self._sink.append((room_id, state, question_event_id))

    async def start_turn(self, room_id):  # type: ignore[no-untyped-def]
        anchor = f"$anchor-{1 + sum(1 for row in self._sink if row[0] == 'start_turn')}"
        self._sink.append(("start_turn", room_id, anchor))
        return anchor

    async def stream_edit(self, room_id, anchor, text, final=False):  # type: ignore[no-untyped-def]
        self._sink.append(("answer", room_id, anchor, text, final))


class _FakeRooms:
    def __init__(self) -> None:
        self.first_titles: list[tuple[str, str]] = []
        self.host_titles: list[tuple[str, str]] = []

    async def maybe_set_first_message_title(self, room_id: str, text: str) -> None:
        self.first_titles.append((room_id, text))

    async def maybe_apply_host_title(self, room_id: str, title: str) -> None:
        self.host_titles.append((room_id, title))


class _FakeSession:
    def __init__(self, rooms: _FakeRooms | None = None) -> None:
        self.rooms = rooms


def _adapter(sink: list[Record], loop: object = None) -> Chat4000MatrixAdapter:
    a = Chat4000MatrixAdapter.__new__(Chat4000MatrixAdapter)
    a._session = _FakeSession()  # truthy / not-None
    a._question_id = {}
    a._html_card_finalized_for_question = {}
    a._status_state = {}
    a._status_task = {}
    a._pending_turns = {}
    a._loop = loop  # type: ignore[assignment]
    a._tw = lambda room_id: _FakeTurnWriter(sink)  # type: ignore[method-assign,assignment]
    return a


def _install_send_result(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import sys
    import types

    base = types.ModuleType("gateway.platforms.base")

    class SendResult:
        def __init__(self, success=True, message_id="", error=None):  # type: ignore[no-untyped-def]
            self.success, self.message_id, self.error = success, message_id, error

    base.SendResult = SendResult  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gateway", types.ModuleType("gateway"))
    monkeypatch.setitem(sys.modules, "gateway.platforms", types.ModuleType("gateway.platforms"))
    monkeypatch.setitem(sys.modules, "gateway.platforms.base", base)


async def test_no_status_without_a_question() -> None:
    sink: list[Record] = []
    a = _adapter(sink)
    await a._status("!r", "thinking")  # no question id recorded → nothing to reference
    assert sink == []


async def test_status_sends_each_transition_no_dedup() -> None:
    sink: list[Record] = []
    a = _adapter(sink)
    a._question_id["!r"] = "$q"
    await a._status("!r", "thinking")
    await a._status("!r", "working")
    await a._status("!r", "working")  # no dedup — every transition is a fresh event
    assert sink == [("!r", "thinking", "$q"), ("!r", "working", "$q"), ("!r", "working", "$q")]


async def test_end_status_sends_idle_once_and_keeps_context() -> None:
    sink: list[Record] = []
    a = _adapter(sink)
    a._question_id["!r"] = "$q"
    await a._status("!r", "working")
    await a._end_status("!r")  # idle exactly once
    assert sink == [("!r", "working", "$q"), ("!r", "idle", "$q")]
    # Context is NOT wiped on end — a rapid follow-up turn that already claimed this
    # room must keep its question_id (else it shows no thinking/tools).
    assert a._question_id["!r"] == "$q"
    # Idempotent: a second end re-sends nothing (status already idle).
    await a._end_status("!r")
    assert sink == [("!r", "working", "$q"), ("!r", "idle", "$q")]


async def test_stop_typing_clears_via_idle() -> None:
    sink: list[Record] = []
    a = _adapter(sink)
    a._question_id["!r"] = "$q"
    await a._status("!r", "typing")
    await a.stop_typing("!r")  # Hermes' turn-end hook
    assert sink[-1] == ("!r", "idle", "$q")


async def test_send_typing_is_noop() -> None:
    sink: list[Record] = []
    a = _adapter(sink)
    a._question_id["!r"] = "$q"
    await a.send_typing("!r")  # native typing removed — must do nothing
    assert sink == []


async def test_keepalive_resends_then_stops(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(ha, "STATUS_KEEPALIVE_S", 0.01)
    sink: list[Record] = []
    a = _adapter(sink, asyncio.get_running_loop())
    a._question_id["!r"] = "$q"
    await a._status("!r", "working")  # initial send + starts the keep-alive task
    await asyncio.sleep(0.05)  # let a few keep-alives fire
    sent_during = len(sink)
    assert sent_during >= 3  # initial + at least two re-sends
    await a._end_status("!r")  # cancels the task + sends idle
    await asyncio.sleep(0.03)
    # no more "working" after idle (task is cancelled)
    assert sink[-1] == ("!r", "idle", "$q")
    assert sum(1 for s in sink if s[1] == "working") == sent_during


async def test_followup_turn_status_survives_interrupted_turn_end() -> None:
    """Rapid follow-up: turn2 claims the room, then the interrupted turn1's
    _end_status fires. turn2's context must survive so its later reasoning/tool
    callbacks still emit (the no-thinking/no-tools bug)."""
    sink: list[Record] = []
    a = _adapter(sink)
    a._question_id["!r"] = "$q2"  # turn2's question now owns the room
    await a._status("!r", "thinking")  # turn2 opened
    sink.clear()
    await a._end_status("!r")  # turn1 (interrupted) tears down
    # turn2's context is intact → a later turn2 callback still emits status.
    await a._status("!r", "working")
    assert ("!r", "working", "$q2") in sink


async def test_active_send_pushes_only_when_stop_typing_fires(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """During a Hermes turn, send() opens the Matrix anchor with push:false.
    stop_typing() is the real turn-end hook, so it sends the one push:true edit
    and then idles."""
    _install_send_result(monkeypatch)
    sink: list[Record] = []
    a = _adapter(sink)
    a._question_id["!r"] = "$q"
    await a._status("!r", "thinking")
    sink.clear()
    await a.send("!r", "the answer")
    assert ("answer", "!r", "$anchor-1", "the answer", False) in sink
    assert ("answer", "!r", "$anchor-1", "the answer", True) not in sink
    assert ("!r", "idle", "$q") not in sink

    await a.stop_typing("!r")
    assert ("answer", "!r", "$anchor-1", "the answer", True) in sink
    assert ("!r", "idle", "$q") in sink


async def test_out_of_turn_send_still_pushes_immediately(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _install_send_result(monkeypatch)
    sink: list[Record] = []
    a = _adapter(sink)
    await a.send("!r", "system notice")
    assert ("answer", "!r", "$anchor-1", "system notice", True) in sink
    assert a._pending_turns == {}


async def test_edit_message_streams_without_pushing_until_stop_typing(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _install_send_result(monkeypatch)
    sink: list[Record] = []
    a = _adapter(sink)
    a._question_id["!r"] = "$q"
    await a._status("!r", "thinking")
    sink.clear()

    sent = await a.send("!r", "part")
    await a.edit_message("!r", sent.message_id, "full answer", finalize=True)
    assert ("answer", "!r", "$anchor-1", "full answer", False) in sink
    assert ("answer", "!r", "$anchor-1", "full answer", True) not in sink

    await a.stop_typing("!r")
    assert ("answer", "!r", "$anchor-1", "full answer", True) in sink


async def test_tool_boundary_finalize_does_not_create_push(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _install_send_result(monkeypatch)
    sink: list[Record] = []
    a = _adapter(sink)
    a._question_id["!r"] = "$q"
    await a._status("!r", "thinking")
    sink.clear()

    first = await a.send("!r", "I will check")
    await a.edit_message("!r", first.message_id, "I will check", finalize=True)
    second = await a.send("!r", "Final answer")
    await a.stop_typing("!r")

    assert second.message_id == "$anchor-2"
    assert ("answer", "!r", "$anchor-1", "I will check", True) not in sink
    assert ("answer", "!r", "$anchor-2", "Final answer", True) in sink


async def test_stop_typing_finalizes_pending_turn_once(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _install_send_result(monkeypatch)
    sink: list[Record] = []
    a = _adapter(sink)
    a._question_id["!r"] = "$q"
    await a._status("!r", "thinking")
    sink.clear()

    await a.send("!r", "done")
    await a.stop_typing("!r")
    await a.stop_typing("!r")
    final_edits = [row for row in sink if row == ("answer", "!r", "$anchor-1", "done", True)]
    assert final_edits == [("answer", "!r", "$anchor-1", "done", True)]


def test_room_for_session_routes_by_session_not_global() -> None:
    """The wrong-room-tools fix: a tool's room is resolved from the FIRING session
    (session_id → room_id), never a shared/global room — so two concurrent sessions
    can't bleed tools into each other."""
    a = Chat4000MatrixAdapter.__new__(Chat4000MatrixAdapter)
    a._room_by_session = {
        "agent:main:chat4000:dm:!ynet": "!ynet",
        "agent:main:chat4000:dm:!hi": "!hi",
    }
    # Each session resolves to ITS OWN room (no shared/global room is consulted).
    assert a._room_for_session("agent:main:chat4000:dm:!ynet") == "!ynet"
    assert a._room_for_session("agent:main:chat4000:dm:!hi") == "!hi"
    # A decorated/extended session id still recovers the embedded room.
    assert a._room_for_session("agent:main:chat4000:dm:!ynet:run42") == "!ynet"
    # Unknown session → None: there is deliberately NO shared-room fallback, which
    # would leak the chip into the wrong room under concurrency.
    assert a._room_for_session("agent:main:chat4000:dm:!gone") is None
    # Empty session id → None too (no crash, no misroute).
    assert a._room_for_session("") is None


async def test_first_message_title_delegates_to_rooms() -> None:
    rooms = _FakeRooms()
    a = _adapter([])
    a._session = _FakeSession(rooms)  # type: ignore[assignment]
    await a._maybe_set_first_message_title("!r", "Please fix login. It fails")
    assert rooms.first_titles == [("!r", "Please fix login. It fails")]


async def test_host_title_callback_delegates_to_rooms() -> None:
    rooms = _FakeRooms()
    a = _adapter([])
    a._session = _FakeSession(rooms)  # type: ignore[assignment]
    await a._apply_host_session_title("!r", "AI title")
    assert rooms.host_titles == [("!r", "AI title")]
