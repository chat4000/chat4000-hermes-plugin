"""Exponential-backoff reconnect loop. Port of TS src/reconnect.ts.

The contract: `run_with_reconnect` blocks until either the abort signal
fires or `should_reconnect(err)` returns False. On every connection
failure, sleeps for `delay = min(initial * 2**attempt + jitter, max)`.
Reset on a successful connect (no exception bubbling out of connect_fn).
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from collections.abc import Awaitable, Callable


async def run_with_reconnect(
    connect_fn: Callable[[], Awaitable[None]],
    *,
    abort_signal: asyncio.Event | None = None,
    initial_delay_secs: float = 2.0,
    max_delay_secs: float = 60.0,
    jitter_ratio: float = 0.2,
    on_error: Callable[[BaseException], None] | None = None,
    on_reconnect: Callable[[float], None] | None = None,
    should_reconnect: Callable[[BaseException], bool] = lambda _e: True,
) -> None:
    retry_delay = initial_delay_secs
    while abort_signal is None or not abort_signal.is_set():
        try:
            await connect_fn()
            retry_delay = initial_delay_secs  # clean exit
        except BaseException as exc:  # noqa: BLE001  # reconnect-loop boundary: surfaces via on_error, honors abort, then retries
            if abort_signal is not None and abort_signal.is_set():
                return
            if on_error is not None:
                try:
                    on_error(exc)
                except Exception as cb_exc:  # noqa: BLE001
                    # User-supplied callback; must not break the reconnect loop.
                    from .error_log import dump_chat4000_trace

                    dump_chat4000_trace("reconnect.on_error", cb_exc)
            if not should_reconnect(exc):
                return
            jitter = retry_delay * jitter_ratio * (random.random() * 2 - 1)  # noqa: S311  # non-security backoff jitter
            delay_with_jitter = max(initial_delay_secs, retry_delay + jitter)
            if on_reconnect is not None:
                try:
                    on_reconnect(delay_with_jitter)
                except Exception as cb_exc:  # noqa: BLE001
                    # User-supplied callback; must not break the reconnect loop.
                    from .error_log import dump_chat4000_trace

                    dump_chat4000_trace("reconnect.on_reconnect", cb_exc)
            await _sleep(delay_with_jitter, abort_signal)
            retry_delay = min(retry_delay * 2, max_delay_secs)


async def _sleep(secs: float, abort_signal: asyncio.Event | None) -> None:
    if abort_signal is None:
        await asyncio.sleep(secs)
        return
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(abort_signal.wait(), timeout=secs)
