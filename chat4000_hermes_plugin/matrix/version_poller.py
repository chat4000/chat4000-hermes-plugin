"""Resident plugin-version poller (protocol C.5.2 `POST /plugin-version`).

The gateway-resident plugin periodically asks the registrar which exact plugin
build it should be running and where to install it from. Per the C.5.2 caller
rule it either already matches that version (no-op) or upgrades to it.

The registrar's `source` field is the WHOLE installer invocation to run — e.g.
`curl … | bash -s -- --hermes-branch <ref> --no-pair --stage`. The poller runs it
VERBATIM; our installer does the actual install AND the gateway restart. The
poller never composes a command or self-installs — the registrar config owns the
full command (including the env, e.g. `--stage` on the stage registrar).

Where this sits in C.5's "when to check" guidance: C.5 says the plugin checks on
boot and before lifecycle/privileged calls, and **must not poll the message
path**. This poller is a low-frequency background timer that NEVER rides the
message path: each tick is a single HTTP call against the registrar, and the
installer launch — which restarts the gateway and so interrupts work — is
DEFERRED until no agent turn / relay is in flight (`is_busy`).

Cadence is env-gated (`registrar_config.resolve_env()`):
  - stage       → 60 s (fast feedback while iterating)
  - production  → 3600 s / 1 h (low-noise steady state)

Robustness: a failed/unreachable check logs and retries on the next tick; it
never crashes the plugin.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from collections.abc import Callable
from typing import Protocol

from ..error_log import dump_chat4000_trace
from ..package_info import read_package_version
from ..registrar_config import resolve_env
from .registrar_client import PluginVersion

logger = logging.getLogger(__name__)

# Env-gated cadence (C.5.2 is a low-frequency background check, never the message
# path). resolve_env() returns "stage" or "production"; anything else falls back
# to the production cadence (the safe, low-noise default).
STAGE_POLL_INTERVAL_S = 60.0
PROD_POLL_INTERVAL_S = 3600.0

# When an upgrade is pending but a turn is in flight, re-check this often for a
# quiet window rather than waiting a full poll interval.
DEFER_RECHECK_INTERVAL_S = 5.0


def poll_interval_for_env(env: str) -> float:
    """Resolve the C.5.2 poll cadence for an environment: stage 60 s, prod 1 h."""
    return STAGE_POLL_INTERVAL_S if env == "stage" else PROD_POLL_INTERVAL_S


class PluginVersionClient(Protocol):
    """The slice of RegistrarClient the poller needs (C.5.2)."""

    async def plugin_version(
        self, app_id: str, *, client_id: str | None = ...
    ) -> PluginVersion: ...


class VersionPoller:
    """Background task: poll `POST /plugin-version` and, when the running build
    differs from the registrar's `current_version`, run the registrar-provided
    installer command (`source`) to upgrade — deferring the launch until no turn
    is in flight (the installer restarts the gateway, which would interrupt work).

    Lifecycle mirrors CompletionListener: `start()` spawns the loop on the running
    event loop (idempotent), `stop()` cancels it cleanly. A single bad tick (network
    blip, registrar 5xx, callback bug) is reported once and never kills the loop.
    """

    def __init__(
        self,
        *,
        app_id: str,
        registrar: PluginVersionClient,
        client_id: str | None,
        is_busy: Callable[[], bool],
        launch_installer: Callable[[str], bool] | None = None,
        installed_version: Callable[[], str] = read_package_version,
        poll_interval_s: float | None = None,
        defer_recheck_interval_s: float = DEFER_RECHECK_INTERVAL_S,
    ) -> None:
        self._app_id = app_id
        self._registrar = registrar
        self._client_id = client_id
        # `is_busy` honours C.5's "not on the message path": True while any agent
        # turn / relay is in flight, so the installer (which restarts the gateway)
        # never launches mid-work.
        self._is_busy = is_busy
        # `launch_installer` runs the registrar's `source` command verbatim; the
        # installer it launches does the install AND the gateway restart.
        self._launch_installer = (
            launch_installer if launch_installer is not None else _spawn_installer
        )
        self._installed_version = installed_version
        self._poll_interval_s = (
            poll_interval_s if poll_interval_s is not None else poll_interval_for_env(resolve_env())
        )
        self._defer_recheck_interval_s = defer_recheck_interval_s
        self._task: asyncio.Task[None] | None = None
        # The installer command the registrar gave us, waiting on a quiet window to
        # launch (the launch restarts the gateway).
        self._pending_source: str | None = None

    @property
    def poll_interval_s(self) -> float:
        return self._poll_interval_s

    # ─── lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the poll loop on the running event loop (idempotent)."""
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

    # ─── poll loop ──────────────────────────────────────────────────────────

    async def _run(self) -> None:
        while True:
            try:
                await self._check_once()
            except Exception as exc:  # noqa: BLE001
                # Poller boundary: one bad tick (network/registrar/callback) must
                # not kill the resident version check — report once, keep going.
                dump_chat4000_trace("matrix.version_poller_tick", exc)
            # If an upgrade is pending on a quiet window, re-check for that window
            # sooner than a full poll interval.
            await asyncio.sleep(
                self._defer_recheck_interval_s
                if self._pending_source is not None
                else self._poll_interval_s
            )

    async def _check_once(self) -> None:
        # An upgrade is pending on a quiet window: don't re-poll the registrar, just
        # retry the deferred launch.
        if self._pending_source is not None:
            self._try_deferred_launch()
            return

        result = await self._registrar.plugin_version(self._app_id, client_id=self._client_id)
        installed = self._installed_version()

        # C.5.2 caller rule: be EXACTLY `current_version`, else run `source` to
        # upgrade into it. Equal → nothing to do.
        if installed == result.current_version:
            logger.debug(
                "plugin-version: installed %s == current %s — no action",
                installed,
                result.current_version,
            )
            return

        logger.info(
            "plugin-version: installed %s != current %s — upgrading via installer: %s",
            installed,
            result.current_version,
            result.source,
        )
        self._pending_source = result.source
        self._try_deferred_launch()

    def _try_deferred_launch(self) -> None:
        """Run the registrar's installer command, but only when no turn is in flight
        (C.5 "not on the message path"; the installer restarts the gateway). While
        busy, leave `_pending_source` set so the next tick retries."""
        if self._is_busy():
            logger.info(
                "plugin-version: upgrade pending, deferring installer launch (turn in flight)"
            )
            return
        source = self._pending_source
        # Clear before launching: the launch restarts the gateway and kills this
        # process; if the launch itself fails, the next poll re-evaluates from scratch.
        self._pending_source = None
        if source is None:
            return
        logger.info("plugin-version: launching installer to upgrade")
        if not self._launch_installer(source):
            logger.warning("plugin-version: installer launch failed — will re-evaluate next tick")


def _spawn_installer(source: str) -> bool:
    """Run the registrar-provided installer command (`source`, the C.5.2 install
    source) VERBATIM, detached. `source` is the whole installer invocation — e.g.
    `curl … | bash -s -- --hermes-branch <ref> --no-pair --stage` — which installs
    the target build AND restarts the gateway. That restart kills THIS process, so
    we detach with `start_new_session=True` (setsid) and redirect std streams to
    /dev/null so the installer survives our own gateway restart. Returns False only
    if the process could not even be launched (the tick re-evaluates next time)."""
    try:
        subprocess.Popen(  # noqa: S602  # `source` is trusted registrar config, run verbatim by design
            source,
            shell=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        logger.warning("plugin-version: could not launch installer for %s: %s", source, exc)
        return False
    return True
