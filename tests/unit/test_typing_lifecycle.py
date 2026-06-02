"""Native m.typing lifecycle (protocol 67919b9 — chat4000.status removed).

One typing on/off per turn: on at the start, refreshed before the 30s timeout
while active, off the moment the turn ends — and Hermes' keep-typing loop can't
re-light a finished room.

The adapter is Hermes-host-coupled, so we build a bare instance via __new__ and
drive the pure typing methods with a fake turn writer + a controllable clock.
"""

from __future__ import annotations

from chat4000_hermes_plugin.matrix.hermes_adapter import (
    TYPING_REFRESH_S,
    Chat4000MatrixAdapter,
)


class _FakeLoop:
    def __init__(self) -> None:
        self.t = 0.0

    def time(self) -> float:
        return self.t


class _FakeTurnWriter:
    def __init__(self, sink: list[tuple[str, str]]) -> None:
        self._sink = sink

    async def set_typing(self, room_id: str, *, typing: bool, timeout_ms: int = 30000) -> None:
        self._sink.append((room_id, "on" if typing else "off"))


def _adapter(sink: list[tuple[str, str]], loop: _FakeLoop | None = None) -> Chat4000MatrixAdapter:
    a = Chat4000MatrixAdapter.__new__(Chat4000MatrixAdapter)
    a._session = object()  # truthy / not-None
    a._typing_on = set()
    a._typing_at = {}
    a._loop = loop  # type: ignore[assignment]
    a._tw = lambda room_id: _FakeTurnWriter(sink)  # type: ignore[method-assign,assignment]
    return a


async def test_send_typing_noop_without_active_turn() -> None:
    sink: list[tuple[str, str]] = []
    a = _adapter(sink)
    await a.send_typing("!r")  # Hermes keep-typing fires but no turn is active
    assert sink == []


async def test_begin_sets_typing_on_and_refresh_throttled() -> None:
    sink: list[tuple[str, str]] = []
    a = _adapter(sink, _FakeLoop())
    await a._begin_turn("!r")
    await a.send_typing("!r")  # same instant → throttled, no second PUT
    assert sink == [("!r", "on")]
    assert "!r" in a._typing_on


async def test_refresh_re_puts_after_window() -> None:
    sink: list[tuple[str, str]] = []
    loop = _FakeLoop()
    a = _adapter(sink, loop)
    await a._begin_turn("!r")  # on @ t=0
    loop.t = TYPING_REFRESH_S + 1.0  # past the refresh window, still inside 30s
    await a.send_typing("!r")  # re-PUT on to keep it alive
    assert sink == [("!r", "on"), ("!r", "on")]


async def test_stop_typing_clears_and_blocks_relight() -> None:
    sink: list[tuple[str, str]] = []
    a = _adapter(sink, _FakeLoop())
    await a._begin_turn("!r")
    await a.stop_typing("!r")  # Hermes' turn-end hook
    assert sink == [("!r", "on"), ("!r", "off")]
    assert "!r" not in a._typing_on
    # keep-typing fires again after teardown — must NOT re-light typing
    sink.clear()
    await a.send_typing("!r")
    assert sink == []


async def test_end_turn_clears_once() -> None:
    sink: list[tuple[str, str]] = []
    a = _adapter(sink, _FakeLoop())
    await a._begin_turn("!r")
    await a._end_turn("!r")
    await a._end_turn("!r")  # already off → no extra PUT
    assert sink == [("!r", "on"), ("!r", "off")]
