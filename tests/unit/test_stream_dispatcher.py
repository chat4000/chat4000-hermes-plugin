"""Streaming text dispatcher (§6.4.2). Pins:
  - fresh wire inner.id per frame
  - stream_id stays stable across delta/end
  - one stream_id per logical reply
  - Bug A: two on_final → two distinct stream_ids
  - Bug B: non-monotonic partial → text_end{reset:true} then new stream_id
"""

from __future__ import annotations

import asyncio

import pytest

from src.dispatch.stream_dispatcher import StreamDispatcher
from src.protocol_types import OutboundTextDelta, OutboundTextEnd


@pytest.fixture
def sent_msgs():
    return []


@pytest.fixture
def dispatcher(sent_msgs):
    return StreamDispatcher(
        send=lambda msg: sent_msgs.append(msg),
        flush_min_chars=10,
        flush_delay_ms=20,
    )


class TestFirstChunk:
    @pytest.mark.asyncio
    async def test_first_partial_emits_immediately(self, dispatcher, sent_msgs):
        await dispatcher.on_partial("hello")
        assert len(sent_msgs) == 1
        assert isinstance(sent_msgs[0], OutboundTextDelta)
        assert sent_msgs[0].delta == "hello"

    @pytest.mark.asyncio
    async def test_empty_partial_is_no_op(self, dispatcher, sent_msgs):
        await dispatcher.on_partial("")
        assert sent_msgs == []


class TestMonotonicExtending:
    @pytest.mark.asyncio
    async def test_prefix_extending_emits_only_delta(self, dispatcher, sent_msgs):
        await dispatcher.on_partial("hello")
        await dispatcher.on_partial("hello world")
        await dispatcher.flush()
        # First chunk emits "hello" immediately. Second chunk is buffered
        # (length 6 < flush_min_chars=10) and ships on flush().
        assert len(sent_msgs) == 2
        assert sent_msgs[0].delta == "hello"
        assert sent_msgs[1].delta == " world"

    @pytest.mark.asyncio
    async def test_exact_repeat_is_no_op(self, dispatcher, sent_msgs):
        await dispatcher.on_partial("hello")
        await dispatcher.on_partial("hello")  # exact dup
        assert len(sent_msgs) == 1


class TestStreamId:
    @pytest.mark.asyncio
    async def test_stream_id_stable_across_deltas(self, dispatcher, sent_msgs):
        await dispatcher.on_partial("foo")
        await dispatcher.on_partial("foobar" * 5)  # forces flush
        await dispatcher.flush()
        delta_msgs = [m for m in sent_msgs if isinstance(m, OutboundTextDelta)]
        # All deltas share one stream_id.
        assert len({m.stream_id for m in delta_msgs}) == 1

    @pytest.mark.asyncio
    async def test_stream_id_stable_through_text_end(self, dispatcher, sent_msgs):
        await dispatcher.on_partial("hello")
        await dispatcher.on_final("hello world")
        delta = next(m for m in sent_msgs if isinstance(m, OutboundTextDelta))
        end = next(m for m in sent_msgs if isinstance(m, OutboundTextEnd))
        assert delta.stream_id == end.stream_id


class TestOnFinal:
    @pytest.mark.asyncio
    async def test_streamed_returns_streamed(self, dispatcher, sent_msgs):
        await dispatcher.on_partial("hello")
        result = await dispatcher.on_final("hello world")
        assert result == "streamed"
        ends = [m for m in sent_msgs if isinstance(m, OutboundTextEnd)]
        assert len(ends) == 1
        assert ends[0].text == "hello world"
        assert ends[0].reset is False

    @pytest.mark.asyncio
    async def test_oneshot_when_no_streaming(self, dispatcher):
        result = await dispatcher.on_final("complete reply")
        assert result == "oneshot"

    @pytest.mark.asyncio
    async def test_empty_when_no_streaming_and_no_text(self, dispatcher):
        result = await dispatcher.on_final("")
        assert result == "empty"

    @pytest.mark.asyncio
    async def test_empty_when_no_streaming_and_only_whitespace(self, dispatcher):
        result = await dispatcher.on_final("   \n  ")
        assert result == "empty"


class TestRotation:
    """Bug A pin (2026-05-05): two `on_final` calls in one agent run
    used to produce two `text_end` frames on the same stream_id. Fixed
    by rotating state at the end of every on_final()."""

    @pytest.mark.asyncio
    async def test_second_on_final_uses_fresh_stream_id(self, dispatcher, sent_msgs):
        await dispatcher.on_partial("first")
        await dispatcher.on_final("first reply")

        await dispatcher.on_partial("second")
        await dispatcher.on_final("second reply")

        ends = [m for m in sent_msgs if isinstance(m, OutboundTextEnd)]
        assert len(ends) == 2
        # Two distinct stream_ids — no protocol-§6.4.2 violation.
        assert ends[0].stream_id != ends[1].stream_id

    @pytest.mark.asyncio
    async def test_partial_after_final_starts_new_stream(self, dispatcher, sent_msgs):
        await dispatcher.on_partial("a")
        first_stream_id = dispatcher.current_stream_id()
        await dispatcher.on_final("a")
        # Now a fresh on_partial should be a brand-new stream.
        await dispatcher.on_partial("b")
        assert dispatcher.current_stream_id() != first_stream_id


class TestNonMonotonicReset:
    """Bug B pin (2026-05-05): a non-monotonic partial mid-stream used to
    emit a text_delta with backwards content. Fix: emit text_end{reset:true}
    on the abandoned stream_id, then start a fresh stream_id with the new
    text."""

    @pytest.mark.asyncio
    async def test_rewrite_emits_reset_text_end(self, dispatcher, sent_msgs):
        await dispatcher.on_partial("hello world")
        # Agent backtracks and rewrites with something that's not a prefix.
        await dispatcher.on_partial("goodbye")
        ends = [m for m in sent_msgs if isinstance(m, OutboundTextEnd)]
        assert len(ends) == 1
        assert ends[0].reset is True

    @pytest.mark.asyncio
    async def test_rewrite_then_new_stream_id(self, dispatcher, sent_msgs):
        await dispatcher.on_partial("hello world")
        first_id = sent_msgs[0].stream_id
        await dispatcher.on_partial("goodbye")
        # After the reset, the next delta uses a fresh stream_id.
        deltas = [m for m in sent_msgs if isinstance(m, OutboundTextDelta)]
        assert len(deltas) >= 2
        assert deltas[1].stream_id != first_id

    @pytest.mark.asyncio
    async def test_reset_callback_fires(self, sent_msgs):
        resets = []
        d = StreamDispatcher(
            send=lambda m: sent_msgs.append(m),
            on_stream_reset=lambda info: resets.append(info),
            flush_min_chars=1000,
            flush_delay_ms=100,
        )
        await d.on_partial("hello world")
        await d.on_partial("goodbye")
        assert len(resets) == 1
        assert resets[0].abandoned_chars > 0


class TestDispose:
    @pytest.mark.asyncio
    async def test_dispose_makes_subsequent_calls_no_op(self, dispatcher, sent_msgs):
        await dispatcher.on_partial("hello")
        dispatcher.dispose()
        await dispatcher.on_partial("world")
        # Only the first chunk made it out.
        assert len(sent_msgs) == 1

    @pytest.mark.asyncio
    async def test_dispose_idempotent(self, dispatcher):
        dispatcher.dispose()
        dispatcher.dispose()  # second call must not raise
