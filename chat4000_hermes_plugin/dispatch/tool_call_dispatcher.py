"""Tool-call dispatcher — Hermes-specific, NEW for chat4000 v1.

Mirrors `StreamDispatcher` but for agent tool invocations instead of text.
Wraps Hermes' tool-call lifecycle hooks and emits three new wire frames:

  - tool_start: { tool_id, name, args }                  — once per call
  - tool_delta: { tool_id, delta }                        — optional, streamed stdout
  - tool_end:   { tool_id, status, result, duration_ms }  — once per call

Same correlation model as text streaming:
  - tool_id is the stable correlator (analog of body.stream_id)
  - Each wire frame gets a fresh inner.id per §6.4.2
  - Receivers dedupe by inner.id (§6.6.9) and merge by tool_id

Argument / result truncation:
  - args truncated to ~2 KB on the wire (tools like write_file can dump
    100 KB into args; truncation keeps the chat-bubble UX usable)
  - result truncated to ~4 KB; the full result lives in the agent's
    transcript and can be requested via a follow-up RPC (v2)
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from ..protocol_types import OutboundToolDelta, OutboundToolEnd, OutboundToolStart, ToolStatus

# Wire-frame size caps. Chosen to fit comfortably inside the relay's
# 64 KB max envelope after b64 + AEAD inflation (~1.37×).
ARGS_TRUNCATE_BYTES = 2048
RESULT_TRUNCATE_BYTES = 4096

# Throttle for tool_delta — coalesce stdout/intermediate output into one
# frame per 100 ms instead of one frame per stdout line.
DELTA_FLUSH_DELAY_MS = 100
DELTA_FLUSH_MIN_CHARS = 256


SendCallback = Callable[
    [OutboundToolStart | OutboundToolDelta | OutboundToolEnd],
    Awaitable[None] | None,
]


@dataclass
class _ToolState:
    """Per-tool-invocation state. One entry per active tool_id."""
    tool_id: str
    name: str
    started_at_ms: int
    delta_buffer: str = ""
    delta_flush_task: Optional[asyncio.TimerHandle] = None
    closed: bool = False


class ToolCallDispatcher:
    """One dispatcher per agent run — like StreamDispatcher.

    Hermes' agent pipeline calls on_tool_start when it decides to invoke
    a tool, on_tool_output (optional, streaming) while the tool runs, and
    on_tool_end when the tool returns. This class translates those into
    wire frames the chat4000 client renders.

    Concurrent tools: Hermes can run multiple tools in parallel (e.g.
    parallel web searches). We key state by tool_id so concurrent
    invocations don't trample each other."""

    def __init__(self, *, send: SendCallback):
        self._send = send
        self._tools: dict[str, _ToolState] = {}
        self._disposed = False

    # ─── Hermes lifecycle hooks (called from adapter.py) ──────────────────

    async def on_tool_start(self, *, name: str, args: dict | str) -> str:
        """Emit tool_start. Returns the tool_id the caller threads through
        on_tool_output / on_tool_end. Hermes' agent doesn't natively expose
        a per-invocation id — we mint our own UUID so concurrent tool
        invocations don't collide on (name) alone."""
        if self._disposed:
            return ""
        tool_id = str(uuid.uuid4())
        state = _ToolState(
            tool_id=tool_id,
            name=name,
            started_at_ms=int(time.time() * 1000),
        )
        self._tools[tool_id] = state

        args_str = self._encode_args(args)
        await self._send_or_await(
            OutboundToolStart(tool_id=tool_id, name=name, args=args_str)
        )
        return tool_id

    async def on_tool_output(self, tool_id: str, delta: str) -> None:
        """Optional — call when a long-running tool emits intermediate
        output (shell stdout, web-search progress, etc.). Coalesced to
        ~100 ms / ~256 char frames to keep the wire from getting hammered.

        Safe to skip entirely; fast tools just go from tool_start →
        tool_end with no deltas in between."""
        if self._disposed or not delta:
            return
        state = self._tools.get(tool_id)
        if state is None or state.closed:
            return
        state.delta_buffer += delta
        if len(state.delta_buffer) >= DELTA_FLUSH_MIN_CHARS:
            await self._flush_delta(state)
            return
        self._schedule_delta_flush(state)

    async def on_tool_end(
        self,
        tool_id: str,
        *,
        status: ToolStatus = "done",
        result: str = "",
    ) -> None:
        """Emit tool_end. The caller passes the tool's summarized result
        (or stderr on failure). Closes the tool_id slot so subsequent
        on_tool_output calls are dropped."""
        if self._disposed:
            return
        state = self._tools.get(tool_id)
        if state is None or state.closed:
            return
        # Flush any pending delta before the terminal frame.
        await self._flush_delta(state)
        state.closed = True
        duration_ms = max(0, int(time.time() * 1000) - state.started_at_ms)

        result_truncated = self._truncate_bytes(result, RESULT_TRUNCATE_BYTES)
        await self._send_or_await(
            OutboundToolEnd(
                tool_id=tool_id,
                status=status,
                result=result_truncated,
                duration_ms=duration_ms,
            )
        )
        # Drop state so a stuck tool_id doesn't leak memory across a
        # multi-hour gateway lifetime.
        self._tools.pop(tool_id, None)

    async def flush_all(self) -> None:
        """Drain pending delta buffers without closing the tools. Used
        between agent turns so partial output doesn't sit in memory."""
        if self._disposed:
            return
        for state in list(self._tools.values()):
            if not state.closed:
                await self._flush_delta(state)

    def dispose(self) -> None:
        """Stop scheduling flushes. Pending state is dropped — caller
        should call on_tool_end on each open tool_id first if it wants
        clean terminal frames."""
        self._disposed = True
        for state in self._tools.values():
            if state.delta_flush_task is not None:
                try:
                    state.delta_flush_task.cancel()
                except Exception:
                    pass
                state.delta_flush_task = None
        self._tools.clear()

    # ─── Internals ────────────────────────────────────────────────────────

    async def _flush_delta(self, state: _ToolState) -> None:
        if state.delta_flush_task is not None:
            try:
                state.delta_flush_task.cancel()
            except Exception:
                pass
            state.delta_flush_task = None
        if not state.delta_buffer:
            return
        delta = state.delta_buffer
        state.delta_buffer = ""
        await self._send_or_await(
            OutboundToolDelta(tool_id=state.tool_id, delta=delta)
        )

    def _schedule_delta_flush(self, state: _ToolState) -> None:
        if state.delta_flush_task is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        state.delta_flush_task = loop.call_later(
            DELTA_FLUSH_DELAY_MS / 1000.0,
            lambda: asyncio.ensure_future(self._scheduled_flush(state)),
        )

    async def _scheduled_flush(self, state: _ToolState) -> None:
        state.delta_flush_task = None
        await self._flush_delta(state)

    @staticmethod
    def _encode_args(args: dict | str) -> str:
        if isinstance(args, str):
            return ToolCallDispatcher._truncate_bytes(args, ARGS_TRUNCATE_BYTES)
        try:
            encoded = json.dumps(args, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            encoded = str(args)
        return ToolCallDispatcher._truncate_bytes(encoded, ARGS_TRUNCATE_BYTES)

    @staticmethod
    def _truncate_bytes(s: str, max_bytes: int) -> str:
        """Truncate `s` so its UTF-8 representation is at most max_bytes,
        without splitting a multi-byte codepoint. Adds a '…' marker when
        truncation actually happens so the client UI can show a hint."""
        encoded = s.encode("utf-8")
        if len(encoded) <= max_bytes:
            return s
        # Walk back to a codepoint boundary.
        cut = encoded[:max_bytes]
        # UTF-8 continuation bytes start with 10xxxxxx; back off until we
        # find a leading byte.
        while cut and (cut[-1] & 0xC0) == 0x80:
            cut = cut[:-1]
        try:
            return cut.decode("utf-8") + "…"
        except UnicodeDecodeError:
            return cut.decode("utf-8", errors="replace") + "…"

    async def _send_or_await(self, msg) -> None:
        result = self._send(msg)
        if asyncio.iscoroutine(result):
            await result
