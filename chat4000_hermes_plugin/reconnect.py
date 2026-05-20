"""Exponential-backoff reconnect loop. Port of TS src/reconnect.ts.

The contract: `run_with_reconnect` blocks until either the abort signal
fires or `should_reconnect(err)` returns False. On every connection
failure, sleeps for `delay = min(initial * 2**attempt + jitter, max)`.
Reset on a successful connect (no exception bubbling out of connect_fn).
"""

from __future__ import annotations

import asyncio
import random
from typing import Awaitable, Callable, Optional


async def run_with_reconnect(
    connect_fn: Callable[[], Awaitable[None]],
    *,
    abort_signal: Optional[asyncio.Event] = None,
    initial_delay_secs: float = 2.0,
    max_delay_secs: float = 60.0,
    jitter_ratio: float = 0.2,
    on_error: Optional[Callable[[BaseException], None]] = None,
    on_reconnect: Optional[Callable[[float], None]] = None,
    should_reconnect: Callable[[BaseException], bool] = lambda _e: True,
) -> None:
    retry_delay = initial_delay_secs
    while abort_signal is None or not abort_signal.is_set():
        try:
            await connect_fn()
            retry_delay = initial_delay_secs  # clean exit
        except BaseException as exc:
            if abort_signal is not None and abort_signal.is_set():
                return
            if on_error is not None:
                try:
                    on_error(exc)
                except Exception:
                    pass
            if not should_reconnect(exc):
                return
            jitter = retry_delay * jitter_ratio * (random.random() * 2 - 1)
            delay_with_jitter = max(initial_delay_secs, retry_delay + jitter)
            if on_reconnect is not None:
                try:
                    on_reconnect(delay_with_jitter)
                except Exception:
                    pass
            await _sleep(delay_with_jitter, abort_signal)
            retry_delay = min(retry_delay * 2, max_delay_secs)


async def _sleep(secs: float, abort_signal: Optional[asyncio.Event]) -> None:
    if abort_signal is None:
        await asyncio.sleep(secs)
        return
    try:
        await asyncio.wait_for(abort_signal.wait(), timeout=secs)
    except asyncio.TimeoutError:
        pass
