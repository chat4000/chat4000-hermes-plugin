"""Flow A `recv_ack` debouncer — per protocol §6.6.3.

Port of clawconnect-plugin/src/recv-ack-batcher.ts. Emits a single
`recv_ack` frame whichever of these fires first:
  - 32 newly persisted seqs are pending
  - 50 ms have elapsed since the most recent persistence
  - explicit `flush_now()` / `shutdown()` on disconnect

The watermark is persisted to SQLite BEFORE the recv_ack envelope is
shipped, so a crash between send and fsync never leaves us re-acking
a seq we may not have persisted. (Same crash-safety invariant as TS.)
"""

from __future__ import annotations

import asyncio
import bisect
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from .ack_store import Chat4000AckStore


@dataclass
class RecvAckBatcherOptions:
    group_id: str
    store: Chat4000AckStore
    send: Callable[[dict], Awaitable[None] | None]  # ships an outer envelope
    role: str = "plugin"
    count_threshold: int = 32
    idle_flush_ms: int = 50
    max_ranges: int = 32  # protocol §6.6.3 caps at 256; we batch at 32


class RecvAckBatcher:
    """Single-account batcher. The transport owns one per active connection
    and calls `shutdown()` on disconnect (clean) or socket teardown."""

    def __init__(self, opts: RecvAckBatcherOptions):
        self._opts = opts
        # Sorted list of distinct persisted seqs strictly above the watermark.
        self._pending: list[int] = []
        self._pending_count = 0
        self._high_water = opts.store.get_last_acked_seq(opts.group_id, opts.role)
        self._timer: Optional[asyncio.TimerHandle] = None
        self._closed = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def record_persisted(self, seq: int) -> None:
        """Called by the transport after persisting an inbound msg.
        Folds contiguous seqs into the watermark; isolated seqs become
        out-of-order ranges in the next emission."""
        if self._closed:
            return
        if not isinstance(seq, int) or seq <= 0:
            return
        if seq <= self._high_water:
            # Already covered; emit a refresh anyway so the relay (which may
            # have lost track during a reconnect dance) drops the duplicate.
            self._schedule_flush()
            return

        idx = bisect.bisect_left(self._pending, seq)
        if idx < len(self._pending) and self._pending[idx] == seq:
            return  # exact duplicate, drop
        self._pending.insert(idx, seq)
        self._pending_count += 1

        # Fold any contiguous run starting at watermark+1 into the watermark.
        while self._pending and self._pending[0] == self._high_water + 1:
            self._high_water = self._pending.pop(0)

        if self._pending_count >= self._opts.count_threshold:
            self.flush_now(reason="count")
            return
        self._schedule_flush()

    def flush_now(self, reason: str = "manual") -> None:
        """Synchronous trigger — fires off the actual send via the event loop."""
        if self._closed:
            return
        if self._timer is not None:
            try:
                self._timer.cancel()
            except Exception:
                pass
            self._timer = None
        # Schedule the actual ship on the loop so we don't block the caller.
        loop = self._get_loop()
        if loop is not None and loop.is_running():
            loop.call_soon(lambda: asyncio.ensure_future(self._emit(reason)))
        else:
            # No running loop — call directly (test path).
            try:
                coro = self._emit(reason)
                if asyncio.iscoroutine(coro):
                    try:
                        asyncio.get_event_loop().run_until_complete(coro)
                    except Exception:
                        pass
            except Exception:
                pass

    def shutdown(self) -> None:
        """Final flush + stop accepting new seqs. Idempotent."""
        if self._closed:
            return
        self.flush_now(reason="shutdown")
        self._closed = True
        if self._timer is not None:
            try:
                self._timer.cancel()
            except Exception:
                pass
            self._timer = None

    # ─── Internals ────────────────────────────────────────────────────────

    def _schedule_flush(self) -> None:
        if self._timer is not None:
            return
        loop = self._get_loop()
        if loop is None or not loop.is_running():
            return
        self._timer = loop.call_later(
            self._opts.idle_flush_ms / 1000.0,
            lambda: self.flush_now(reason="idle"),
        )

    def _get_loop(self) -> Optional[asyncio.AbstractEventLoop]:
        if self._loop is not None:
            return self._loop
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                self._loop = asyncio.get_event_loop()
            except RuntimeError:
                return None
        return self._loop

    async def _emit(self, reason: str) -> None:
        if self._closed and reason != "shutdown":
            return
        persisted_watermark = self._opts.store.get_last_acked_seq(
            self._opts.group_id, self._opts.role
        )
        if (
            self._high_water <= persisted_watermark
            and not self._pending
            and self._pending_count == 0
        ):
            return  # nothing new to ack
        self._pending_count = 0

        # Collapse leftover (out-of-order) seqs into [low, high] ranges.
        ranges: list[tuple[int, int]] = []
        if self._pending:
            run_start = self._pending[0]
            run_end = run_start
            for s in self._pending[1:]:
                if s == run_end + 1:
                    run_end = s
                else:
                    ranges.append((run_start, run_end))
                    if len(ranges) >= self._opts.max_ranges:
                        break
                    run_start = s
                    run_end = s
            else:
                ranges.append((run_start, run_end))
        trimmed = ranges[: self._opts.max_ranges]

        payload: dict = {"up_to_seq": self._high_water}
        if trimmed:
            payload["ranges"] = [list(r) for r in trimmed]

        # Persist watermark BEFORE the envelope ships — crash safety.
        self._opts.store.set_last_acked_seq(
            self._opts.group_id, self._high_water, self._opts.role
        )

        envelope = {"version": 1, "type": "recv_ack", "payload": payload}
        try:
            result = self._opts.send(envelope)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            # Best-effort. If we fail to send, the next reconnect's hello will
            # carry the persisted watermark and the relay will re-deliver.
            pass

    # ─── Test inspection ──────────────────────────────────────────────────

    def state_for_tests(self) -> dict:
        return {"high_water": self._high_water, "pending": list(self._pending)}
