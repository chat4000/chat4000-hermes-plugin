"""Tool-call dispatcher — the NEW feature for Hermes support.

Pins the wire-frame contract:
  - tool_start emitted once per invocation with our minted tool_id
  - tool_delta coalesced (256 chars / 100 ms)
  - tool_end emitted once with status + duration_ms
  - concurrent tools keyed by tool_id don't collide
  - args truncated to ~2 KB, result to ~4 KB
"""

from __future__ import annotations

import asyncio
import json

import pytest

from chat4000_hermes_plugin.dispatch.tool_call_dispatcher import (
    ARGS_TRUNCATE_BYTES,
    RESULT_TRUNCATE_BYTES,
    ToolCallDispatcher,
)
from chat4000_hermes_plugin.protocol_types import OutboundToolDelta, OutboundToolEnd, OutboundToolStart


@pytest.fixture
def sent():
    return []


@pytest.fixture
def dispatcher(sent):
    return ToolCallDispatcher(send=lambda msg: sent.append(msg))


class TestToolStart:
    @pytest.mark.asyncio
    async def test_emits_tool_start(self, dispatcher, sent):
        tid = await dispatcher.on_tool_start(name="bash", args="ls -la")
        assert tid != ""
        starts = [m for m in sent if isinstance(m, OutboundToolStart)]
        assert len(starts) == 1
        assert starts[0].name == "bash"
        assert starts[0].args == "ls -la"
        assert starts[0].tool_id == tid

    @pytest.mark.asyncio
    async def test_dict_args_get_json_encoded(self, dispatcher, sent):
        await dispatcher.on_tool_start(name="web", args={"query": "hermes"})
        start = next(m for m in sent if isinstance(m, OutboundToolStart))
        # JSON shape — order-stable per our separators kwarg.
        assert json.loads(start.args) == {"query": "hermes"}

    @pytest.mark.asyncio
    async def test_concurrent_tools_get_unique_ids(self, dispatcher):
        a = await dispatcher.on_tool_start(name="bash", args="echo a")
        b = await dispatcher.on_tool_start(name="bash", args="echo b")
        c = await dispatcher.on_tool_start(name="web", args="q1")
        assert len({a, b, c}) == 3

    @pytest.mark.asyncio
    async def test_args_truncated_when_too_large(self, dispatcher, sent):
        huge = "x" * (ARGS_TRUNCATE_BYTES * 3)
        await dispatcher.on_tool_start(name="bash", args=huge)
        start = next(m for m in sent if isinstance(m, OutboundToolStart))
        # Truncated to <= ARGS_TRUNCATE_BYTES + 1 char for the … marker.
        assert len(start.args.encode("utf-8")) <= ARGS_TRUNCATE_BYTES + 4
        assert start.args.endswith("…")

    @pytest.mark.asyncio
    async def test_disposed_returns_empty_id(self):
        d = ToolCallDispatcher(send=lambda m: None)
        d.dispose()
        tid = await d.on_tool_start(name="x", args="")
        assert tid == ""


class TestToolDeltaCoalescing:
    @pytest.mark.asyncio
    async def test_short_delta_buffered(self, dispatcher, sent):
        tid = await dispatcher.on_tool_start(name="bash", args="x")
        await dispatcher.on_tool_output(tid, "hello\n")
        # Below the 256-char threshold — buffered, not yet shipped.
        deltas = [m for m in sent if isinstance(m, OutboundToolDelta)]
        assert deltas == []

    @pytest.mark.asyncio
    async def test_large_delta_flushes_immediately(self, dispatcher, sent):
        tid = await dispatcher.on_tool_start(name="bash", args="x")
        await dispatcher.on_tool_output(tid, "a" * 300)
        deltas = [m for m in sent if isinstance(m, OutboundToolDelta)]
        assert len(deltas) == 1
        assert deltas[0].tool_id == tid

    @pytest.mark.asyncio
    async def test_idle_timer_flushes_buffered(self, dispatcher, sent):
        tid = await dispatcher.on_tool_start(name="bash", args="x")
        await dispatcher.on_tool_output(tid, "short")
        # Wait past 100ms idle threshold.
        await asyncio.sleep(0.2)
        deltas = [m for m in sent if isinstance(m, OutboundToolDelta)]
        assert len(deltas) == 1

    @pytest.mark.asyncio
    async def test_output_on_unknown_tool_id_is_no_op(self, dispatcher, sent):
        await dispatcher.on_tool_output("not-a-real-id", "ignored")
        assert sent == []

    @pytest.mark.asyncio
    async def test_output_after_end_is_no_op(self, dispatcher, sent):
        tid = await dispatcher.on_tool_start(name="bash", args="x")
        await dispatcher.on_tool_end(tid, status="done", result="")
        await dispatcher.on_tool_output(tid, "late")
        deltas = [m for m in sent if isinstance(m, OutboundToolDelta)]
        assert deltas == []


class TestToolEnd:
    @pytest.mark.asyncio
    async def test_emits_tool_end_with_duration(self, dispatcher, sent):
        tid = await dispatcher.on_tool_start(name="bash", args="x")
        await asyncio.sleep(0.05)  # ~50ms
        await dispatcher.on_tool_end(tid, status="done", result="ok")
        end = next(m for m in sent if isinstance(m, OutboundToolEnd))
        assert end.tool_id == tid
        assert end.status == "done"
        assert end.result == "ok"
        assert end.duration_ms >= 30  # >= the sleep, minus slop

    @pytest.mark.asyncio
    async def test_result_truncated(self, dispatcher, sent):
        tid = await dispatcher.on_tool_start(name="bash", args="x")
        huge_result = "y" * (RESULT_TRUNCATE_BYTES * 3)
        await dispatcher.on_tool_end(tid, status="done", result=huge_result)
        end = next(m for m in sent if isinstance(m, OutboundToolEnd))
        assert len(end.result.encode("utf-8")) <= RESULT_TRUNCATE_BYTES + 4
        assert end.result.endswith("…")

    @pytest.mark.asyncio
    async def test_status_failed_passes_through(self, dispatcher, sent):
        tid = await dispatcher.on_tool_start(name="bash", args="x")
        await dispatcher.on_tool_end(tid, status="failed", result="permission denied")
        end = next(m for m in sent if isinstance(m, OutboundToolEnd))
        assert end.status == "failed"

    @pytest.mark.asyncio
    async def test_end_on_unknown_tool_id_is_no_op(self, dispatcher, sent):
        await dispatcher.on_tool_end("not-real", status="done", result="x")
        ends = [m for m in sent if isinstance(m, OutboundToolEnd)]
        assert ends == []

    @pytest.mark.asyncio
    async def test_double_end_is_no_op(self, dispatcher, sent):
        tid = await dispatcher.on_tool_start(name="bash", args="x")
        await dispatcher.on_tool_end(tid, status="done", result="")
        await dispatcher.on_tool_end(tid, status="done", result="")
        ends = [m for m in sent if isinstance(m, OutboundToolEnd)]
        assert len(ends) == 1

    @pytest.mark.asyncio
    async def test_end_flushes_pending_delta_first(self, dispatcher, sent):
        tid = await dispatcher.on_tool_start(name="bash", args="x")
        await dispatcher.on_tool_output(tid, "buffered")  # below threshold
        await dispatcher.on_tool_end(tid, status="done", result="final")
        # The pending delta should have shipped BEFORE the end frame.
        types = [type(m).__name__ for m in sent]
        # tool_start → tool_delta → tool_end ordering preserved.
        assert types.index("OutboundToolDelta") < types.index("OutboundToolEnd")


class TestUtf8SafeTruncation:
    """Truncation walks back to a codepoint boundary so multi-byte chars
    aren't sliced in half."""

    @pytest.mark.asyncio
    async def test_truncate_multibyte(self, dispatcher, sent):
        # Emoji is 4 bytes in UTF-8. Build a string that lands the cut
        # inside a codepoint.
        emoji = "🤖"
        s = "x" * (ARGS_TRUNCATE_BYTES - 2) + emoji * 4
        await dispatcher.on_tool_start(name="x", args=s)
        start = next(m for m in sent if isinstance(m, OutboundToolStart))
        # Must still be valid UTF-8 (no broken codepoint).
        start.args.encode("utf-8")  # no exception
        # Cap respected.
        assert len(start.args.encode("utf-8")) <= ARGS_TRUNCATE_BYTES + 4


class TestFlushAll:
    @pytest.mark.asyncio
    async def test_flush_all_drains_pending(self, dispatcher, sent):
        a = await dispatcher.on_tool_start(name="bash", args="x")
        b = await dispatcher.on_tool_start(name="web", args="y")
        await dispatcher.on_tool_output(a, "buf-a")
        await dispatcher.on_tool_output(b, "buf-b")
        await dispatcher.flush_all()
        deltas = [m for m in sent if isinstance(m, OutboundToolDelta)]
        # Both tools' buffered output flushed.
        assert {d.tool_id for d in deltas} == {a, b}


class TestDispose:
    @pytest.mark.asyncio
    async def test_dispose_clears_state(self, dispatcher, sent):
        tid = await dispatcher.on_tool_start(name="bash", args="x")
        dispatcher.dispose()
        # Subsequent calls are no-ops.
        await dispatcher.on_tool_output(tid, "ignored")
        await dispatcher.on_tool_end(tid, status="done", result="")
        deltas = [m for m in sent if isinstance(m, OutboundToolDelta)]
        ends = [m for m in sent if isinstance(m, OutboundToolEnd)]
        assert deltas == [] and ends == []
