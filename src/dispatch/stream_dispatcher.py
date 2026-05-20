"""Streaming text dispatcher — enforces protocol §6.4.2 invariants.

Port of clawconnect-plugin/src/stream-dispatcher.ts. Same buffering rules
(200 chars / 100 ms idle flush), same monotonic-prefix detection, same
reset-on-rewrite behaviour.

The §6.4.2 invariants this layer enforces:
  - One stream_id per logical reply. Never reuse a stream_id after text_end.
  - When the agent backtracks (non-monotonic partial), close the current
    stream with text_end{reset:true} and mint a fresh stream_id.
  - Each wire frame gets a fresh inner.id (UUID v4). The stream_id lives
    in body.stream_id, NOT in inner.id.

Pinned production bugs (same as TS):
  - Bug A: two onFinal() calls within one agent run → 2 text_end on one
    stream_id. Fixed by `onFinal` rotating state at the end of every call.
  - Bug B: non-monotonic partial mid-stream → backwards-content text_delta.
    Fixed by detecting non-monotonic input and emitting reset:true.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal, Optional

from ..types import OutboundTextDelta, OutboundTextEnd

DEFAULT_FLUSH_MIN_CHARS = 200
DEFAULT_FLUSH_DELAY_MS = 100


@dataclass
class StreamResetInfo:
    stream_id: str
    abandoned_chars: int


SendCallback = Callable[
    [OutboundTextDelta | OutboundTextEnd], Awaitable[None] | None
]
ResetCallback = Callable[[StreamResetInfo], None]


class StreamDispatcher:
    """One dispatcher per agent run (i.e. per inbound user prompt).

    Holds the current stream's text + the fresh stream_id. `on_final`
    rotates state so the NEXT `on_partial` starts a fresh stream_id —
    this is what makes multi-reply agent runs safe."""

    def __init__(
        self,
        *,
        send: SendCallback,
        on_stream_reset: Optional[ResetCallback] = None,
        flush_min_chars: int = DEFAULT_FLUSH_MIN_CHARS,
        flush_delay_ms: int = DEFAULT_FLUSH_DELAY_MS,
    ):
        self._send = send
        self._on_stream_reset = on_stream_reset
        self._flush_min_chars = flush_min_chars
        self._flush_delay_ms = flush_delay_ms

        self._stream_id = str(uuid.uuid4())
        self._stream_active = False
        self._last_text = ""
        # The MOST RECENT partial we received. Used to detect monotonic-
        # extending vs rewrite-mid-stream input. Cleared on every rotation.
        self._last_partial_text = ""
        self._buffer = ""
        self._first_chunk_sent = False
        self._flush_task: Optional[asyncio.TimerHandle] = None
        self._disposed = False

    def current_stream_id(self) -> str:
        return self._stream_id

    def is_active(self) -> bool:
        return self._stream_active

    async def on_partial(self, text: str) -> None:
        """Receive a partial reply from the agent. Implements:
          - first chunk → emit immediately (no buffering on leading edge)
          - exact repeat → no-op
          - prefix-extending → emit only the appended slice
          - non-monotonic (rewrite) → reset stream, mint new stream_id"""
        if self._disposed or not text:
            return
        if not self._last_partial_text:
            self._last_partial_text = text
            await self._queue_delta(text)
            return
        if text == self._last_partial_text:
            return
        if text.startswith(self._last_partial_text):
            delta = text[len(self._last_partial_text):]
            self._last_partial_text = text
            await self._queue_delta(delta)
            return
        # Non-monotonic — the agent backtracked.
        self._last_partial_text = text
        await self._reset_for_rewrite(text)

    async def on_final(
        self, text: str
    ) -> Literal["streamed", "oneshot", "empty"]:
        """Receive a deliver(kind=final) from the reply pipeline.
        Returns:
          - "streamed": text_end emitted on the active stream
          - "empty":    no active stream and text was empty (agent silent)
          - "oneshot":  no streaming had happened but text is present —
                        caller should send a single `text` frame instead"""
        if self._disposed:
            return "empty"
        if self._stream_active:
            await self._flush_buffer()
            final_text = text or self._last_text
            if final_text:
                await self._send_or_await(
                    OutboundTextEnd(
                        stream_id=self._stream_id, text=final_text, reset=False
                    )
                )
            self._rotate()
            return "streamed"
        if not text.strip():
            self._rotate()
            return "empty"
        self._rotate()
        return "oneshot"

    async def flush(self) -> None:
        """Drain pending buffered delta. The buffer normally flushes on
        count/idle thresholds; this is for explicit drain between
        on_partial and on_final."""
        if self._disposed:
            return
        await self._flush_buffer()

    def dispose(self) -> None:
        """Stop scheduling flushes. Pending buffer is dropped; an in-flight
        text_end is the caller's responsibility (call on_final first).
        Idempotent."""
        self._disposed = True
        self._clear_flush_timer()
        self._buffer = ""

    # ─── Internals ────────────────────────────────────────────────────────

    async def _queue_delta(self, delta: str) -> None:
        if not delta:
            return
        if not self._first_chunk_sent:
            self._first_chunk_sent = True
            self._stream_active = True
            self._last_text += delta
            await self._send_or_await(
                OutboundTextDelta(stream_id=self._stream_id, delta=delta)
            )
            return
        self._buffer += delta
        if len(self._buffer) >= self._flush_min_chars:
            await self._flush_buffer()
            return
        self._schedule_flush()

    async def _flush_buffer(self) -> None:
        self._clear_flush_timer()
        if not self._buffer:
            return
        delta = self._buffer
        self._buffer = ""
        self._stream_active = True
        self._last_text += delta
        await self._send_or_await(
            OutboundTextDelta(stream_id=self._stream_id, delta=delta)
        )

    async def _reset_for_rewrite(self, next_text: str) -> None:
        """Close the abandoned stream with reset:true (the iPhone deletes
        that bubble), then mint a fresh stream_id and replay the new text."""
        self._clear_flush_timer()
        self._buffer = ""
        if self._stream_active and self._last_text:
            abandoned_stream_id = self._stream_id
            abandoned_chars = len(self._last_text)
            await self._send_or_await(
                OutboundTextEnd(
                    stream_id=abandoned_stream_id,
                    text=self._last_text,
                    reset=True,
                )
            )
            if self._on_stream_reset is not None:
                try:
                    self._on_stream_reset(
                        StreamResetInfo(
                            stream_id=abandoned_stream_id,
                            abandoned_chars=abandoned_chars,
                        )
                    )
                except Exception:
                    pass
        self._stream_id = str(uuid.uuid4())
        self._stream_active = False
        self._last_text = ""
        self._first_chunk_sent = False
        if next_text:
            await self._queue_delta(next_text)

    def _rotate(self) -> None:
        """Reset state so the NEXT deliver(final) starts on a fresh
        stream_id. Fires once per element of the agent's `replies` array."""
        self._clear_flush_timer()
        self._stream_id = str(uuid.uuid4())
        self._stream_active = False
        self._last_text = ""
        self._last_partial_text = ""
        self._buffer = ""
        self._first_chunk_sent = False

    def _schedule_flush(self) -> None:
        if self._flush_task is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._flush_task = loop.call_later(
            self._flush_delay_ms / 1000.0,
            lambda: asyncio.ensure_future(self._scheduled_flush()),
        )

    async def _scheduled_flush(self) -> None:
        self._flush_task = None
        await self._flush_buffer()

    def _clear_flush_timer(self) -> None:
        if self._flush_task is not None:
            try:
                self._flush_task.cancel()
            except Exception:
                pass
            self._flush_task = None

    async def _send_or_await(self, msg) -> None:
        result = self._send(msg)
        if asyncio.iscoroutine(result):
            await result
