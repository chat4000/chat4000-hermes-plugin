"""Resident plugin-version poller (protocol C.5.2 `POST /plugin-version`).

The gateway-resident plugin periodically asks the registrar which exact plugin
build it should be running and where to install it from, then — per the C.5.2
caller rule — either already matches that version (no-op) or installs `source`
and restarts into it. This is the *install-source* check (C.5.2), distinct from
the policy/nag/force check (`POST /version`, C.5.1) the CLI runs on boot.

Where this sits in C.5's "when to check" guidance: C.5 says the plugin checks on
boot and before lifecycle/privileged calls, and **must not poll the message
path**. This poller is a low-frequency background timer that NEVER rides the
message path: each tick is a single HTTP call against the registrar, and a
reinstall/restart that would interrupt work is DEFERRED until no agent turn /
relay is in flight (`is_busy`).

Cadence is env-gated (`registrar_config.resolve_env()`):
  - stage       → 60 s (fast feedback while iterating)
  - production  → 3600 s / 1 h (low-noise steady state)

Robustness: a failed/unreachable check logs and retries on the next tick; it
never crashes the plugin. The reinstall is the known pip gotcha —
`--upgrade --force-reinstall` so a pinned/older `source` actually re-installs
(plain `--upgrade` will not downgrade).
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
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

# When a reinstall is pending but a turn is in flight, re-check this often for a
# quiet window rather than waiting a full poll interval.
DEFER_RECHECK_INTERVAL_S = 5.0

# pip's own network/index work can be slow; cap it so a wedged install can't hang
# the poller's loop forever (the gateway restart is what actually swaps versions).
_PIP_INSTALL_TIMEOUT_S = 600.0


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
    differs from the registrar's `current_version`, install `source` and restart
    into it — deferring the disruptive part until no turn is in flight.

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
        restart: Callable[[], None],
        install: Callable[[str], bool] | None = None,
        installed_version: Callable[[], str] = read_package_version,
        poll_interval_s: float | None = None,
        defer_recheck_interval_s: float = DEFER_RECHECK_INTERVAL_S,
    ) -> None:
        self._app_id = app_id
        self._registrar = registrar
        self._client_id = client_id
        # `is_busy` honours C.5's "not on the message path": True while any agent
        # turn / relay is in flight, so a reinstall+restart never interrupts work.
        self._is_busy = is_busy
        # `restart` swaps the running gateway into the freshly-installed build.
        self._restart = restart
        self._install = install if install is not None else _pip_force_reinstall
        self._installed_version = installed_version
        self._poll_interval_s = (
            poll_interval_s if poll_interval_s is not None else poll_interval_for_env(resolve_env())
        )
        self._defer_recheck_interval_s = defer_recheck_interval_s
        self._task: asyncio.Task[None] | None = None
        # The build the registrar told us to run while we were busy — installed +
        # waiting on a quiet window to restart into.
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
            # If a reinstall is installed-but-waiting on a quiet window, re-check
            # for that window sooner than a full poll interval.
            await asyncio.sleep(
                self._defer_recheck_interval_s
                if self._pending_source is not None
                else self._poll_interval_s
            )

    async def _check_once(self) -> None:
        # A reinstall already happened and is waiting on a quiet window: don't
        # re-poll the registrar, just retry the deferred restart.
        if self._pending_source is not None:
            self._try_deferred_restart()
            return

        result = await self._registrar.plugin_version(self._app_id, client_id=self._client_id)
        current_version = result.current_version
        source = result.source
        installed = self._installed_version()

        # C.5.2 caller rule: be EXACTLY `current_version`, else install `source`
        # and restart into it. Equal → nothing to do.
        if installed == current_version:
            logger.debug(
                "plugin-version: installed %s == current %s — no action",
                installed,
                current_version,
            )
            return

        logger.info(
            "plugin-version: installed %s != current %s — installing %s",
            installed,
            current_version,
            source,
        )
        if not self._install(source):
            logger.warning("plugin-version: install of %s failed — will retry next tick", source)
            return
        # Installed; the restart is the disruptive part — defer it past any turn.
        self._pending_source = source
        self._try_deferred_restart()

    def _try_deferred_restart(self) -> None:
        """Restart into the freshly-installed build, but only when no turn is in
        flight (C.5 "not on the message path"). While busy, leave `_pending_source`
        set so the next tick retries."""
        if self._is_busy():
            logger.info("plugin-version: new build installed, deferring restart (turn in flight)")
            return
        logger.info("plugin-version: restarting into freshly-installed build")
        self._pending_source = None
        self._restart()


def _pip_force_reinstall(source: str) -> bool:
    """Install `source` into THIS interpreter's environment (the plugin venv) via
    pip. `--upgrade --force-reinstall` is required: plain `--upgrade` will not
    *downgrade* to a pinned/older `source`, and the registrar can pin any exact
    build (C.5.2), so we always force the install to match. Returns True on
    success; logs + returns False on a pip failure (the tick retries later)."""
    args = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--upgrade",
        "--force-reinstall",
        source,
    ]
    try:
        proc = subprocess.run(  # noqa: S603  # fixed argv; `source` is registrar-controlled config
            args,
            capture_output=True,
            text=True,
            timeout=_PIP_INSTALL_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("plugin-version: pip install of %s raised: %s", source, exc)
        return False
    if proc.returncode != 0:
        logger.warning(
            "plugin-version: pip install of %s exited %d: %s",
            source,
            proc.returncode,
            (proc.stderr or "").strip()[-500:],
        )
        return False
    return True


def restart_gateway() -> None:
    """Restart the running Hermes gateway so it reloads the freshly-installed
    plugin build. Reuses the install-wizard's restart shape: kill `hermes gateway
    run`; a supervisor (systemd / docker restart policy / launchd) brings it back.

    If the gateway is NOT supervised it will not come back on its own — that is the
    operator's deployment choice (the installer's wizard documents the supervised
    expectation); the resident plugin cannot daemonize a replacement of its own host
    safely, so we log the unsupervised case loudly rather than fork a detached
    process from inside the host we are killing."""
    try:
        subprocess.run(  # noqa: S603
            ["pkill", "-9", "-f", "hermes gateway run"],  # noqa: S607  # trusted fixed argv (matches install_wizard)
            capture_output=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("plugin-version: could not signal gateway restart: %s", exc)
