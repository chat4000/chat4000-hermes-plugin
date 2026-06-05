"""external_tool_start routing is FAIL-SAFE (no global fallback).

A `chat4000.tool` chip must render in the room of the turn that fired the tool,
resolved from PER-TURN identity (the contextvar `room` arg, or the session→room
map). If neither resolves a room, the chip is DROPPED and the occurrence is
REPORTED to the error sink — it is NEVER routed to the global `_active_room`,
which is last-written by the most recent inbound message and so points at the
wrong room under concurrent turns (a cross-room content leak).

The adapter is Hermes-host-coupled, so we build a bare instance via __new__ and
drive `external_tool_start` with stubs.
"""

from __future__ import annotations

import chat4000_hermes_plugin.error_log as error_log
from chat4000_hermes_plugin.matrix.hermes_adapter import Chat4000MatrixAdapter


class _FakeTurnWriter:
    def __init__(self, sink: list) -> None:
        self._sink = sink

    async def tool_start(self, room_id, *, tool_id, name, icon=""):  # type: ignore[no-untyped-def]
        self._sink.append((room_id, tool_id, name, icon))
        return "$ev"


def _adapter(sink: list, *, session: object | None) -> Chat4000MatrixAdapter:
    a = Chat4000MatrixAdapter.__new__(Chat4000MatrixAdapter)
    a._session = session  # type: ignore[assignment]
    a._room_by_session = {}
    a._active_room = None
    a._tw = lambda room_id: _FakeTurnWriter(sink)  # type: ignore[method-assign,assignment]

    async def _status(room_id, state):  # type: ignore[no-untyped-def]
        sink.append(("status", room_id, state))

    a._status = _status  # type: ignore[method-assign,assignment]
    return a


def test_room_for_session_returns_none_not_global() -> None:
    """No per-turn match → None, even with the global set. The global must never
    be the answer (it would misroute under concurrency)."""
    a = Chat4000MatrixAdapter.__new__(Chat4000MatrixAdapter)
    a._room_by_session = {"agent:main:chat4000:dm:!known": "!known"}
    a._active_room = "!known"  # global is set…
    assert a._room_for_session("") is None  # …empty session → None, not "!known"
    assert a._room_for_session("agent:main:chat4000:dm:!gone") is None  # no match → None


async def test_resolvable_room_emits_one_chip_and_returns_id() -> None:
    sink: list = []
    a = _adapter(sink, session=object())
    tool_id = await a.external_tool_start("web_search", icon="🔎", room="!r")
    # exactly one tool_start, into the given room, with the returned id; then working.
    chips = [s for s in sink if s[0] == "!r"]
    assert chips == [("!r", tool_id, "web_search", "🔎")]
    assert tool_id  # a real correlator, not the dropped ""
    assert ("status", "!r", "working") in sink


async def test_unresolvable_room_drops_chip_and_reports_once(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    reports: list = []
    monkeypatch.setattr(
        error_log,
        "dump_chat4000_trace",
        lambda scope, exc, ctx=None: reports.append((scope, type(exc).__name__, ctx)),
    )
    sink: list = []
    a = _adapter(sink, session=object())  # connected, but no room resolvable
    out = await a.external_tool_start("web_search", session_id="agent:main:chat4000:dm:!gone")
    assert out == ""  # dropped
    assert sink == []  # NO tool event, NO status emitted
    assert len(reports) == 1  # reported exactly once
    scope, exc_name, ctx = reports[0]
    assert scope == "matrix.tool_start_unroutable"
    assert exc_name == "_UnroutableToolStart"
    assert ctx == {"tool_name": "web_search", "session_id": "agent:main:chat4000:dm:!gone"}


async def test_not_connected_returns_empty_without_reporting(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    reports: list = []
    monkeypatch.setattr(
        error_log,
        "dump_chat4000_trace",
        lambda scope, exc, ctx=None: reports.append(scope),
    )
    sink: list = []
    a = _adapter(sink, session=None)  # not connected
    out = await a.external_tool_start("web_search", room="!r")
    assert out == ""
    assert sink == []
    assert reports == []  # not-connected is benign — must NOT report
