"""Pairing-completion listener (protocol C.4 "Completion listening").

The gateway-resident plugin owns pairing completion: this listener polls
`/pair/status` for EVERY outstanding code in the pending-codes store — single-use
and reusable, CLI/installer-registered and `device.pair_start` ones — for each
code's whole lifetime, surviving restarts (the store is the durable state; the
listener re-reads it every scan, so codes registered by a separate CLI process
are picked up live).

Per C.3 there is nothing to do membership-wise on completion (the user's invites
pre-exist from setup, C.6); the listener's jobs per new redeem are delegated to
the adapter via `on_redeem` (record the device / known user — the plugin's normal
NEXT-SEND key share then reaches the new device; keys are never pre-shared) and
`on_transition` (the `chat4000.pair_status` lifecycle for `device.pair_start`
codes after a restart).

Cadence (C.4): the active poll interval while a pairing is actively expected
(the first ~10 min after registration — an install or pair_start window), backing
off to ≥30 s for long-lived codes — a late redeem of a 2-year reusable code is
not latency-sensitive. Transient registrar trouble (429/5xx/network) is already
absorbed inside `RegistrarClient.status()` with exponential backoff; a code whose
status call still fails transiently is simply retried on a later scan.

In-process watchers (the command handler's `device.pair_start` poll, which emits
the live lifecycle events) `claim()` their code so the listener doesn't
double-poll it; an unclean shutdown leaves no claim behind, so after a restart
the listener owns the code again.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from ..error_log import dump_chat4000_trace
from .pair_codes_store import (
    PendingCode,
    add_pending_code,
    load_pending_codes,
    remove_pending_code,
    update_pending_code,
)
from .registrar_client import RegistrarError

logger = logging.getLogger(__name__)

# C.4 cadence: ~1 s while a pairing is actively expected, ≥30 s for long-lived
# codes. "Actively expected" = within ACTIVE_WINDOW_S of registration.
ACTIVE_POLL_INTERVAL_S = 1.5
IDLE_POLL_INTERVAL_S = 30.0
ACTIVE_WINDOW_S = 600.0
SCAN_INTERVAL_S = 1.5

# Settled states a transition callback can receive.
TRANSITION_COMPLETED = "completed"
TRANSITION_EXPIRED = "expired"

# (record, full /pair/status payload, one redeems[] entry) — fired once per NEW redeem.
RedeemCb = Callable[[PendingCode, dict[str, Any], dict[str, Any]], Awaitable[None]]
# (record, "completed" | "expired", full /pair/status payload) — fired when a code settles.
TransitionCb = Callable[[PendingCode, str, dict[str, Any]], Awaitable[None]]


class StatusClient(Protocol):
    async def status(self, code: str) -> dict[str, Any]: ...


class CompletionListener:
    """Polls every outstanding pairing code until it settles. One per adapter."""

    def __init__(
        self,
        *,
        account_id: str,
        registrar: StatusClient,
        on_redeem: RedeemCb,
        on_transition: TransitionCb | None = None,
        scan_interval_s: float = SCAN_INTERVAL_S,
        active_poll_interval_s: float = ACTIVE_POLL_INTERVAL_S,
        idle_poll_interval_s: float = IDLE_POLL_INTERVAL_S,
        active_window_s: float = ACTIVE_WINDOW_S,
    ) -> None:
        self._account_id = account_id
        self._registrar = registrar
        self._on_redeem = on_redeem
        self._on_transition = on_transition
        self._scan_interval_s = scan_interval_s
        self._active_poll_interval_s = active_poll_interval_s
        self._idle_poll_interval_s = idle_poll_interval_s
        self._active_window_s = active_window_s
        self._task: asyncio.Task[None] | None = None
        self._claimed: set[str] = set()
        self._next_poll_at: dict[str, float] = {}  # code → monotonic deadline

    # ─── lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the scan loop on the running event loop (idempotent)."""
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        import contextlib

        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            # Expected: our own cancel — handled, nothing lost.
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # ─── coordination with in-process watchers ────────────────────────────

    def persist(self, record: PendingCode) -> None:
        """Durably record a freshly registered code (C.4: outstanding codes are
        part of the plugin's persistent state)."""
        add_pending_code(record, self._account_id)

    def claim(self, code: str) -> None:
        """An in-process watcher owns this code's lifecycle; the listener skips
        it until `release`/`discard`. Claims are memory-only on purpose — a
        restart clears them, handing the code back to the listener."""
        self._claimed.add(code)

    def release(self, code: str) -> None:
        self._claimed.discard(code)

    def discard(self, code: str) -> None:
        """The code settled (or was cancelled): forget it everywhere."""
        remove_pending_code(code, self._account_id)
        self.release(code)
        self._next_poll_at.pop(code, None)

    # ─── scan loop ────────────────────────────────────────────────────────

    async def _run(self) -> None:
        while True:
            try:
                await self._scan_once()
            except Exception as exc:  # noqa: BLE001
                # Listener boundary: one bad scan (callback bug, store trouble)
                # must not kill completion listening — report once, keep going.
                dump_chat4000_trace("matrix.pair_listener_scan", exc)
            await asyncio.sleep(self._scan_interval_s)

    async def _scan_once(self) -> None:
        now = time.monotonic()
        for record in load_pending_codes(self._account_id):
            if record.code in self._claimed:
                continue
            if now < self._next_poll_at.get(record.code, 0.0):
                continue
            await self._poll_one(record)

    async def _poll_one(self, record: PendingCode) -> None:
        try:
            status = await self._registrar.status(record.code)
        except RegistrarError as exc:
            if exc.is_transient:
                # status() already absorbed its retry budget; try again next scan.
                logger.debug("pair listener: registrar still unavailable (%s)", exc)
                return
            if exc.status == 404:
                # GC'd after the status-retention window (C.4) — nothing more to
                # learn from this code; drop the record.
                logger.info("pair listener: code …%s GC'd by the registrar", record.code[-2:])
                self.discard(record.code)
                return
            # Non-transient and not a GC (bad token, bad param): unexpected.
            # Report once, then back off this code hard rather than hot-loop.
            dump_chat4000_trace(
                "matrix.pair_listener_status", exc, {"code_suffix": record.code[-2:]}
            )
            self._next_poll_at[record.code] = time.monotonic() + self._idle_poll_interval_s
            return

        await self._apply_status(record, status)

    async def _apply_status(self, record: PendingCode, status: dict[str, Any]) -> None:
        state = status.get("status")
        redeems = [e for e in (status.get("redeems") or []) if isinstance(e, dict)]
        count = int(status.get("redeemed_count") or len(redeems))

        if count > record.redeemed_count_seen:
            new_n = count - record.redeemed_count_seen
            # `redeems` is oldest-first and may be truncated to the most recent
            # 20 (C.3) — process the newest `new_n` entries we actually have.
            for entry in redeems[-new_n:]:
                await self._on_redeem(record, status, entry)
            record.redeemed_count_seen = count
            update_pending_code(record, self._account_id)

        if state == "completed":
            self.discard(record.code)
            if self._on_transition is not None:
                await self._on_transition(record, TRANSITION_COMPLETED, status)
            return
        if state == "expired":
            self.discard(record.code)
            if self._on_transition is not None:
                await self._on_transition(record, TRANSITION_EXPIRED, status)
            return

        # Still pending (reusable codes stay pending until expiry, C.3).
        self._next_poll_at[record.code] = time.monotonic() + self._poll_interval(record)

    def _poll_interval(self, record: PendingCode) -> float:
        if record.registered_at_ms:
            age_s = max(0.0, time.time() - record.registered_at_ms / 1000.0)
            if age_s <= self._active_window_s:
                return self._active_poll_interval_s
        return self._idle_poll_interval_s
