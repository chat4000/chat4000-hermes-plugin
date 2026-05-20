"""Flow A `recv_ack` debouncer (§6.6.3) — watermark folding, range
compression, count/idle/shutdown flush triggers, persistence-before-send.

Important: the batcher only folds a seq into `high_water` if it's
contiguous with `high_water + 1`. Isolated seqs stay in `pending` and
emit as `ranges`. Tests scenarios reflect that."""

from __future__ import annotations

import asyncio

import pytest

from src.ack_store import Chat4000AckStore
from src.recv_ack_batcher import RecvAckBatcher, RecvAckBatcherOptions


@pytest.fixture
def store(tmp_path):
    s = Chat4000AckStore(tmp_path / "ack.sqlite")
    yield s
    s.close()


@pytest.fixture
def captured_envelopes():
    return []


@pytest.fixture
def make_batcher(store, captured_envelopes):
    def _make(**overrides) -> RecvAckBatcher:
        def send(env):
            captured_envelopes.append(env)
            return None

        opts = RecvAckBatcherOptions(
            group_id="g1",
            store=store,
            send=send,
            **overrides,
        )
        return RecvAckBatcher(opts)

    return _make


class TestWatermarkFolding:
    @pytest.mark.asyncio
    async def test_contiguous_seqs_fold_into_watermark(self, make_batcher):
        b = make_batcher()
        b.record_persisted(1)
        b.record_persisted(2)
        b.record_persisted(3)
        st = b.state_for_tests()
        assert st["high_water"] == 3
        assert st["pending"] == []

    @pytest.mark.asyncio
    async def test_out_of_order_stays_in_pending(self, make_batcher):
        b = make_batcher()
        b.record_persisted(1)
        b.record_persisted(3)  # gap at 2
        st = b.state_for_tests()
        assert st["high_water"] == 1
        assert st["pending"] == [3]

    @pytest.mark.asyncio
    async def test_gap_fills_folds_into_watermark(self, make_batcher):
        b = make_batcher()
        b.record_persisted(1)
        b.record_persisted(3)
        b.record_persisted(2)
        st = b.state_for_tests()
        assert st["high_water"] == 3
        assert st["pending"] == []

    @pytest.mark.asyncio
    async def test_duplicate_seq_is_no_op(self, make_batcher):
        b = make_batcher()
        b.record_persisted(5)
        b.record_persisted(5)  # exact dup → dropped
        st = b.state_for_tests()
        # 5 isn't contiguous with watermark 0, so it stays in pending.
        # The dup must not double-count.
        assert st["pending"] == [5]

    @pytest.mark.asyncio
    async def test_seq_below_watermark_ignored(self, make_batcher, store):
        store.set_last_acked_seq("g1", 100)
        b = make_batcher()
        b.record_persisted(50)
        st = b.state_for_tests()
        assert st["high_water"] == 100


class TestFlushTriggers:
    @pytest.mark.asyncio
    async def test_count_threshold_flushes(self, make_batcher, captured_envelopes):
        b = make_batcher(count_threshold=3)
        b.record_persisted(1)
        b.record_persisted(2)
        b.record_persisted(3)
        await asyncio.sleep(0.02)
        assert any(e.get("type") == "recv_ack" for e in captured_envelopes)
        env = next(e for e in captured_envelopes if e.get("type") == "recv_ack")
        # All three fold contiguously into the watermark.
        assert env["payload"]["up_to_seq"] == 3

    @pytest.mark.asyncio
    async def test_idle_timer_flushes(self, make_batcher, captured_envelopes):
        b = make_batcher(count_threshold=100, idle_flush_ms=30)
        b.record_persisted(1)
        await asyncio.sleep(0.1)
        assert len(captured_envelopes) >= 1

    @pytest.mark.asyncio
    async def test_shutdown_flushes_and_closes(self, make_batcher, captured_envelopes):
        b = make_batcher()
        b.record_persisted(1)
        b.record_persisted(2)
        b.shutdown()
        await asyncio.sleep(0.05)
        env = next(e for e in captured_envelopes if e.get("type") == "recv_ack")
        assert env["payload"]["up_to_seq"] == 2
        # After shutdown, new seqs are dropped (no new envelopes for 3).
        b.record_persisted(3)
        await asyncio.sleep(0.05)
        after = [
            e for e in captured_envelopes
            if e["payload"].get("up_to_seq") == 3
        ]
        assert after == []


class TestWatermarkPersistedBeforeSend:
    """§6.6.3 invariant: persist watermark BEFORE shipping the envelope.
    A crash between send-and-fsync must not leave us re-acking a seq we
    didn't persist."""

    @pytest.mark.asyncio
    async def test_store_updated_when_envelope_emitted(
        self, make_batcher, store, captured_envelopes
    ):
        b = make_batcher(count_threshold=2)
        b.record_persisted(1)
        b.record_persisted(2)
        await asyncio.sleep(0.05)
        assert store.get_last_acked_seq("g1") == 2

    @pytest.mark.asyncio
    async def test_store_updated_on_shutdown_even_if_send_fails(
        self, store, captured_envelopes
    ):
        def failing_send(env):
            raise RuntimeError("simulated socket failure")

        b = RecvAckBatcher(
            RecvAckBatcherOptions(
                group_id="g1",
                store=store,
                send=failing_send,
            )
        )
        # Contiguous 1 → folds into watermark.
        b.record_persisted(1)
        b.shutdown()
        await asyncio.sleep(0.05)
        # Watermark persisted even though the wire send raised.
        assert store.get_last_acked_seq("g1") == 1


class TestRangeCompression:
    @pytest.mark.asyncio
    async def test_ranges_emitted_for_out_of_order_seqs(
        self, make_batcher, captured_envelopes
    ):
        b = make_batcher(count_threshold=4)
        # Contiguous 1..2 fold into watermark; 5..6 stay as a range.
        b.record_persisted(1)
        b.record_persisted(2)
        b.record_persisted(5)
        b.record_persisted(6)
        await asyncio.sleep(0.05)
        env = next(e for e in captured_envelopes if e.get("type") == "recv_ack")
        payload = env["payload"]
        assert payload["up_to_seq"] == 2
        assert payload.get("ranges") == [[5, 6]]

    @pytest.mark.asyncio
    async def test_max_ranges_cap_respected(self, make_batcher, captured_envelopes):
        b = make_batcher(count_threshold=100, max_ranges=2)
        # Six isolated out-of-order seqs — six ranges would form, cap=2.
        for s in (5, 8, 11, 14, 17, 20):
            b.record_persisted(s)
        # Force flush (no count/idle trigger reached).
        b.flush_now(reason="manual")
        await asyncio.sleep(0.05)
        env = next(e for e in captured_envelopes if e.get("type") == "recv_ack")
        assert len(env["payload"].get("ranges", [])) <= 2


class TestNoOpsWhenNothingPending:
    @pytest.mark.asyncio
    async def test_flush_without_pending_emits_nothing(
        self, make_batcher, captured_envelopes
    ):
        b = make_batcher()
        b.flush_now(reason="manual")
        await asyncio.sleep(0.05)
        assert captured_envelopes == []

    @pytest.mark.asyncio
    async def test_shutdown_without_pending_emits_nothing(
        self, make_batcher, captured_envelopes
    ):
        b = make_batcher()
        b.shutdown()
        await asyncio.sleep(0.05)
        assert captured_envelopes == []
