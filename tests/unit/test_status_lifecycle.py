"""chat4000.status lifecycle (protocol e3d9358).

Live activity is a fresh E2EE chat4000.status timeline event per transition,
referencing the QUESTION event_id, re-sent every 4s as a keep-alive, and ALWAYS
closed with one idle. No native typing.

The adapter is Hermes-host-coupled, so we build a bare instance via __new__ and
drive the pure status methods with a fake turn writer.
"""

from __future__ import annotations

import asyncio

import chat4000_hermes_plugin.matrix.hermes_adapter as ha
from chat4000_hermes_plugin.matrix.hermes_adapter import Chat4000MatrixAdapter


class _FakeTurnWriter:
    def __init__(self, sink: list[tuple[str, str, str]]) -> None:
        self._sink = sink

    async def send_status(self, room_id: str, state: str, question_event_id: str) -> None:
        self._sink.append((room_id, state, question_event_id))

    async def start_turn(self, room_id):  # type: ignore[no-untyped-def]
        self._sink.append(("start_turn", room_id))
        return "$anchor"

    async def stream_edit(self, room_id, anchor, text, final=False):  # type: ignore[no-untyped-def]
        self._sink.append(("answer", text))


def _adapter(sink: list[tuple[str, str, str]], loop: object = None) -> Chat4000MatrixAdapter:
    a = Chat4000MatrixAdapter.__new__(Chat4000MatrixAdapter)
    a._session = object()  # truthy / not-None
    a._question_id = {}
    a._status_state = {}
    a._status_task = {}
    a._loop = loop  # type: ignore[assignment]
    a._tw = lambda room_id: _FakeTurnWriter(sink)  # type: ignore[method-assign,assignment]
    return a


async def test_no_status_without_a_question() -> None:
    sink: list[tuple[str, str, str]] = []
    a = _adapter(sink)
    await a._status("!r", "thinking")  # no question id recorded → nothing to reference
    assert sink == []


async def test_status_sends_each_transition_no_dedup() -> None:
    sink: list[tuple[str, str, str]] = []
    a = _adapter(sink)
    a._question_id["!r"] = "$q"
    await a._status("!r", "thinking")
    await a._status("!r", "working")
    await a._status("!r", "working")  # no dedup — every transition is a fresh event
    assert sink == [("!r", "thinking", "$q"), ("!r", "working", "$q"), ("!r", "working", "$q")]


async def test_end_status_sends_idle_once_and_keeps_context() -> None:
    sink: list[tuple[str, str, str]] = []
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
    sink: list[tuple[str, str, str]] = []
    a = _adapter(sink)
    a._question_id["!r"] = "$q"
    await a._status("!r", "typing")
    await a.stop_typing("!r")  # Hermes' turn-end hook
    assert sink[-1] == ("!r", "idle", "$q")


async def test_send_typing_is_noop() -> None:
    sink: list[tuple[str, str, str]] = []
    a = _adapter(sink)
    a._question_id["!r"] = "$q"
    await a.send_typing("!r")  # native typing removed — must do nothing
    assert sink == []


async def test_keepalive_resends_then_stops(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(ha, "STATUS_KEEPALIVE_S", 0.01)
    sink: list[tuple[str, str, str]] = []
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
    sink: list[tuple[str, str, str]] = []
    a = _adapter(sink)
    a._question_id["!r"] = "$q2"  # turn2's question now owns the room
    await a._status("!r", "thinking")  # turn2 opened
    sink.clear()
    await a._end_status("!r")  # turn1 (interrupted) tears down
    # turn2's context is intact → a later turn2 callback still emits status.
    await a._status("!r", "working")
    assert ("!r", "working", "$q2") in sink


async def test_send_emits_answer_then_idle(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """send() posts the answer (anchor + edit) then idle. START-only means there are
    no tool ENDs to flush — send is just answer → idle."""
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

    sink: list[tuple[str, str, str]] = []
    a = _adapter(sink)
    a._question_id["!r"] = "$q"
    await a.send("!r", "the answer")
    kinds = [s[0] for s in sink]
    assert ("answer", "the answer") in sink  # answer was sent
    assert kinds.index("answer") < kinds.index("!r")  # then idle (the status tuple) last
    assert ("!r", "idle", "$q") in sink


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
