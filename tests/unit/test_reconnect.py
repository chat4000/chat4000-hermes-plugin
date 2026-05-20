"""Exponential-backoff reconnect loop.

The contract: `run_with_reconnect` blocks until either the abort signal
fires or `should_reconnect(err)` returns False. A clean `connect_fn`
return does NOT exit the loop — it loops to call connect_fn again. So
every test below uses an abort_signal to terminate.
"""

from __future__ import annotations

import asyncio

import pytest

from chat4000_hermes_plugin.reconnect import run_with_reconnect


def _schedule_abort(signal: asyncio.Event, delay: float) -> None:
    """Schedule abort_signal.set() on the currently-running test loop.

    Pytest-asyncio creates a fresh event loop per test, so we must use
    `get_running_loop()` (not `get_event_loop()`) to attach the timer to
    the loop the test is actually awaiting on."""
    asyncio.get_running_loop().call_later(delay, signal.set)


class TestAbort:
    @pytest.mark.asyncio
    async def test_abort_signal_exits_loop(self):
        attempts = []

        async def connect():
            attempts.append(1)
            # Sleep so the abort signal has a chance to fire while we wait.
            await asyncio.sleep(0.5)

        signal = asyncio.Event()
        _schedule_abort(signal, 0.05)
        await run_with_reconnect(connect, abort_signal=signal, initial_delay_secs=0.01)
        # At least one connect attempt happened, and the loop exited.
        assert len(attempts) >= 1


class TestRetry:
    @pytest.mark.asyncio
    async def test_retries_on_exception(self):
        attempts = []

        async def connect():
            attempts.append(1)
            if len(attempts) < 3:
                raise RuntimeError("simulated failure")
            await asyncio.sleep(1)  # block so the signal can fire

        signal = asyncio.Event()
        _schedule_abort(signal, 0.3)
        await run_with_reconnect(
            connect,
            abort_signal=signal,
            initial_delay_secs=0.01,
            max_delay_secs=0.05,
        )
        assert len(attempts) >= 3

    @pytest.mark.asyncio
    async def test_on_error_called(self):
        errors = []

        async def connect():
            raise ValueError("boom")

        signal = asyncio.Event()
        _schedule_abort(signal, 0.05)
        await run_with_reconnect(
            connect,
            abort_signal=signal,
            initial_delay_secs=0.01,
            on_error=lambda exc: errors.append(exc),
        )
        assert any(isinstance(e, ValueError) for e in errors)

    @pytest.mark.asyncio
    async def test_on_reconnect_called_with_delay(self):
        delays = []

        async def connect():
            raise RuntimeError("x")

        signal = asyncio.Event()
        _schedule_abort(signal, 0.1)
        await run_with_reconnect(
            connect,
            abort_signal=signal,
            initial_delay_secs=0.01,
            max_delay_secs=0.05,
            on_reconnect=lambda d: delays.append(d),
        )
        assert delays
        # Each delay ≥ initial_delay_secs (the floor enforced inside).
        assert all(d >= 0.01 for d in delays)


class TestShouldReconnect:
    @pytest.mark.asyncio
    async def test_returning_false_stops_loop(self):
        attempts = []

        async def connect():
            attempts.append(1)
            raise ValueError("fatal")

        signal = asyncio.Event()
        _schedule_abort(signal, 0.5)  # safety net only — should_reconnect=False exits first
        await run_with_reconnect(
            connect,
            abort_signal=signal,
            initial_delay_secs=0.01,
            should_reconnect=lambda exc: False,
        )
        # One failure, no retry.
        assert len(attempts) == 1


class TestBackoff:
    @pytest.mark.asyncio
    async def test_initial_delay_floor(self):
        """Delay between attempts must respect the initial_delay_secs floor
        (the jitter calc can't produce a sub-floor wait)."""
        delays = []

        async def connect():
            raise RuntimeError("x")

        signal = asyncio.Event()
        _schedule_abort(signal, 0.2)
        await run_with_reconnect(
            connect,
            abort_signal=signal,
            initial_delay_secs=0.02,
            max_delay_secs=0.1,
            on_reconnect=lambda d: delays.append(d),
        )
        assert all(d >= 0.02 for d in delays)
