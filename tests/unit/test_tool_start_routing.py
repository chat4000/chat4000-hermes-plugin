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
from chat4000_hermes_plugin.matrix.hermes_adapter import Chat4000MatrixAdapter, _PendingTurn


class _FakeTurnWriter:
    def __init__(self, sink: list) -> None:
        self._sink = sink

    async def tool_start(self, room_id, *, tool_id, name, icon=""):  # type: ignore[no-untyped-def]
        self._sink.append((room_id, tool_id, name, icon))
        return "$ev"

    async def html_card(self, room_id, *, html):  # type: ignore[no-untyped-def]
        self._sink.append(("html_card", room_id, html))
        return "$card"

    async def stream_edit(self, room_id, anchor_id, text, *, final):  # type: ignore[no-untyped-def]
        self._sink.append(("stream_edit", room_id, anchor_id, text, final))
        return "$edit"


def _adapter(sink: list, *, session: object | None) -> Chat4000MatrixAdapter:
    a = Chat4000MatrixAdapter.__new__(Chat4000MatrixAdapter)
    a._session = session  # type: ignore[assignment]
    a._room_by_session = {}
    a._session_by_room = {}
    a._active_room = None
    a._question_id = {}
    a._pending_turns = {}
    a._html_card_finalized_for_question = {}
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


async def test_html_card_marks_turn_final_and_suppresses_text_finalizer() -> None:
    sink: list = []
    a = _adapter(sink, session=object())
    a._question_id = {"!r": "$q"}
    a._room_by_session = {"s1": "!r"}
    a._session_by_room = {"!r": "s1"}
    a._pending_turns = {"!r": _PendingTurn(anchor_id="$anchor", latest_text="duplicate")}

    event_id = await a.external_html_card(
        "<article><p>Done</p></article>",
        room="!r",
        session_id="s1",
    )
    await a._finalize_pending_turn("!r")

    assert event_id == "$card"
    assert ("html_card", "!r", "<article><p>Done</p></article>") in sink
    assert not any(item[0] == "stream_edit" for item in sink)
    assert a._pending_turns == {}
