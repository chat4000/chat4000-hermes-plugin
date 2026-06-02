"""chat4000.status lifecycle — typing only during a turn, idle exactly once.

Regression test for the "spinner spins forever" bug: Hermes' base stop_typing is
a no-op, so the adapter MUST override it to write idle, and send_typing (driven by
Hermes' ~2s keep-typing loop) must not re-assert "typing" after a turn ends.

The adapter is Hermes-host-coupled (its __init__ imports gateway.*), so we build a
bare instance via __new__ and exercise the pure status methods with a fake turn
writer — no Hermes process required.
"""

from __future__ import annotations

from chat4000_hermes_plugin.matrix.hermes_adapter import Chat4000MatrixAdapter


class _FakeTurnWriter:
    def __init__(self, sink: list[tuple[str, str]]) -> None:
        self._sink = sink

    async def set_status(self, room_id: str, state: str) -> None:
        self._sink.append((room_id, state))


def _adapter(sink: list[tuple[str, str]]) -> Chat4000MatrixAdapter:
    a = Chat4000MatrixAdapter.__new__(Chat4000MatrixAdapter)
    a._session = object()  # truthy / not-None
    a._last_status = {}
    a._typing_on = set()
    a._tw = lambda room_id: _FakeTurnWriter(sink)  # type: ignore[method-assign,assignment]
    return a


async def test_send_typing_noop_without_active_turn() -> None:
    sink: list[tuple[str, str]] = []
    a = _adapter(sink)
    await a.send_typing("!r")  # Hermes keep-typing fires but no turn is active
    assert sink == []


async def test_send_typing_writes_once_then_dedupes() -> None:
    sink: list[tuple[str, str]] = []
    a = _adapter(sink)
    a._typing_on.add("!r")
    await a.send_typing("!r")
    await a.send_typing("!r")  # ~2s later — deduped, no new state event
    assert sink == [("!r", "typing")]


async def test_stop_typing_writes_idle_and_blocks_further_typing() -> None:
    sink: list[tuple[str, str]] = []
    a = _adapter(sink)
    a._typing_on.add("!r")
    a._last_status["!r"] = "typing"
    await a.stop_typing("!r")  # Hermes' turn-end hook
    assert sink == [("!r", "idle")]
    assert "!r" not in a._typing_on
    # keep-typing fires again after teardown — must NOT flip back to typing
    sink.clear()
    await a.send_typing("!r")
    assert sink == []


async def test_end_turn_idle_is_idempotent() -> None:
    sink: list[tuple[str, str]] = []
    a = _adapter(sink)
    a._typing_on.add("!r")
    await a._end_turn("!r")
    await a._end_turn("!r")  # idle deduped
    assert sink == [("!r", "idle")]
