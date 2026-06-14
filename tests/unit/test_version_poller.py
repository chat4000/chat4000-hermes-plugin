"""Unit tests for the resident plugin-version poller (protocol C.5.2)."""

from __future__ import annotations

from chat4000_hermes_plugin.matrix.registrar_client import PluginVersion
from chat4000_hermes_plugin.matrix.version_poller import (
    PROD_POLL_INTERVAL_S,
    STAGE_POLL_INTERVAL_S,
    VersionPoller,
    poll_interval_for_env,
)

APP_ID = "@chat4000/hermes-plugin"


class FakeRegistrar:
    """Records each plugin_version call and replays a scripted response."""

    def __init__(self, response: PluginVersion) -> None:
        self.response = response
        self.calls: list[tuple[str, str | None]] = []

    async def plugin_version(self, app_id: str, *, client_id: str | None = None) -> PluginVersion:
        self.calls.append((app_id, client_id))
        return self.response


def _poller(
    *,
    reg: FakeRegistrar,
    installed: str,
    is_busy: bool | list[bool] = False,
    install_ok: bool = True,
    client_id: str | None = "agent-abc",
) -> tuple[VersionPoller, dict[str, list]]:
    """Build a poller with recording install/restart callbacks. `is_busy` may be a
    list to script successive busy/quiet states across deferral re-checks."""
    rec: dict[str, list] = {"installed": [], "restarts": 0}  # type: ignore[dict-item]
    rec["restarts"] = 0

    busy_states = is_busy if isinstance(is_busy, list) else None
    busy_iter = iter(busy_states) if busy_states is not None else None

    def busy() -> bool:
        if busy_iter is not None:
            try:
                return next(busy_iter)
            except StopIteration:
                return False
        assert isinstance(is_busy, bool)
        return is_busy

    def install(source: str) -> bool:
        rec["installed"].append(source)
        return install_ok

    def restart() -> None:
        rec["restarts"] += 1  # type: ignore[operator]

    poller = VersionPoller(
        app_id=APP_ID,
        registrar=reg,
        client_id=client_id,
        is_busy=busy,
        restart=restart,
        install=install,
        installed_version=lambda: installed,
    )
    return poller, rec


# ─── env → interval ──────────────────────────────────────────────────────────


def test_stage_interval_is_60s() -> None:
    assert poll_interval_for_env("stage") == 60.0
    assert STAGE_POLL_INTERVAL_S == 60.0


def test_prod_interval_is_3600s() -> None:
    assert poll_interval_for_env("production") == 3600.0
    assert PROD_POLL_INTERVAL_S == 3600.0


def test_unknown_env_falls_back_to_prod_cadence() -> None:
    assert poll_interval_for_env("weird") == PROD_POLL_INTERVAL_S


def test_poll_interval_override_wins() -> None:
    reg = FakeRegistrar(PluginVersion(current_version="1.0.0", source="git+x"))
    poller, _ = _poller(reg=reg, installed="1.0.0")
    poller_override = VersionPoller(
        app_id=APP_ID,
        registrar=reg,
        client_id=None,
        is_busy=lambda: False,
        restart=lambda: None,
        install=lambda _s: True,
        installed_version=lambda: "1.0.0",
        poll_interval_s=7.0,
    )
    assert poller_override.poll_interval_s == 7.0


# ─── C.5.2 request shape ───────────────────────────────────────────────────────


async def test_request_sends_app_id_and_client_id_header() -> None:
    reg = FakeRegistrar(PluginVersion(current_version="2.0.0", source="git+x"))
    poller, _ = _poller(reg=reg, installed="2.0.0", client_id="agent-abc")
    await poller._check_once()
    assert reg.calls == [(APP_ID, "agent-abc")]


# ─── version matches → no-op ───────────────────────────────────────────────────


async def test_version_matches_is_noop() -> None:
    reg = FakeRegistrar(PluginVersion(current_version="3.1.4", source="git+x@v3.1.4"))
    poller, rec = _poller(reg=reg, installed="3.1.4")
    await poller._check_once()
    assert rec["installed"] == []
    assert rec["restarts"] == 0


# ─── version differs → install + restart ───────────────────────────────────────


async def test_version_differs_quiet_installs_and_restarts() -> None:
    src = "git+https://github.com/x/chat4000-hermes-plugin@v9.9.9"
    reg = FakeRegistrar(PluginVersion(current_version="9.9.9", source=src))
    poller, rec = _poller(reg=reg, installed="1.0.0", is_busy=False)
    await poller._check_once()
    assert rec["installed"] == [src]
    assert rec["restarts"] == 1


async def test_version_differs_busy_defers_restart_until_quiet() -> None:
    src = "git+x@v9.9.9"
    reg = FakeRegistrar(PluginVersion(current_version="9.9.9", source=src))
    # Busy on the first check (and its immediate restart attempt), quiet after.
    poller, rec = _poller(reg=reg, installed="1.0.0", is_busy=[True, True])

    # First tick: installs, but defers restart (busy).
    await poller._check_once()
    assert rec["installed"] == [src]
    assert rec["restarts"] == 0
    assert poller._pending_source == src

    # Next tick while still busy: does NOT re-poll the registrar, still deferred.
    await poller._check_once()
    assert len(reg.calls) == 1  # no second /plugin-version poll
    assert rec["restarts"] == 0

    # Next tick once quiet: restarts into the installed build.
    await poller._check_once()
    assert rec["restarts"] == 1
    assert poller._pending_source is None


async def test_install_failure_retries_next_tick() -> None:
    src = "git+x@v9.9.9"
    reg = FakeRegistrar(PluginVersion(current_version="9.9.9", source=src))
    poller, rec = _poller(reg=reg, installed="1.0.0", install_ok=False)
    await poller._check_once()
    # Install attempted but failed → no restart, nothing pending (retries on the
    # next poll, not via the deferred-restart path).
    assert rec["installed"] == [src]
    assert rec["restarts"] == 0
    assert poller._pending_source is None
