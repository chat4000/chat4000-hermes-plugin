"""Unit tests for the resident plugin-version poller (protocol C.5.2)."""

from __future__ import annotations

from chat4000_hermes_plugin.matrix import version_poller as vp
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
    launch_ok: bool = True,
    client_id: str | None = "agent-abc",
) -> tuple[VersionPoller, dict[str, list]]:
    """Build a poller with a recording `launch_installer` callback. `is_busy` may be
    a list to script successive busy/quiet states across deferral re-checks."""
    rec: dict[str, list] = {"launched": []}

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

    def launch(source: str) -> bool:
        rec["launched"].append(source)
        return launch_ok

    poller = VersionPoller(
        app_id=APP_ID,
        registrar=reg,
        client_id=client_id,
        is_busy=busy,
        launch_installer=launch,
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
    reg = FakeRegistrar(PluginVersion(current_version="1.0.0", source="curl … | bash"))
    poller_override = VersionPoller(
        app_id=APP_ID,
        registrar=reg,
        client_id=None,
        is_busy=lambda: False,
        launch_installer=lambda _s: True,
        installed_version=lambda: "1.0.0",
        poll_interval_s=7.0,
    )
    assert poller_override.poll_interval_s == 7.0


# ─── C.5.2 request shape ───────────────────────────────────────────────────────


async def test_request_sends_app_id_and_client_id_header() -> None:
    reg = FakeRegistrar(PluginVersion(current_version="2.0.0", source="curl … | bash"))
    poller, _ = _poller(reg=reg, installed="2.0.0", client_id="agent-abc")
    await poller._check_once()
    assert reg.calls == [(APP_ID, "agent-abc")]


# ─── version matches → no-op ───────────────────────────────────────────────────


async def test_version_matches_is_noop() -> None:
    reg = FakeRegistrar(PluginVersion(current_version="3.1.4", source="curl … | bash"))
    poller, rec = _poller(reg=reg, installed="3.1.4")
    await poller._check_once()
    assert rec["launched"] == []


# ─── version differs → run the installer command verbatim ──────────────────────


async def test_version_differs_quiet_launches_installer() -> None:
    src = "curl -fsSL https://…/install.sh | bash -s -- --hermes-branch v9.9.9 --no-pair --stage"
    reg = FakeRegistrar(PluginVersion(current_version="9.9.9", source=src))
    poller, rec = _poller(reg=reg, installed="1.0.0", is_busy=False)
    await poller._check_once()
    # The poller runs the registrar's `source` verbatim — exactly one launch, and
    # the installer (not the poller) does the install + restart.
    assert rec["launched"] == [src]
    assert poller._pending_source is None


async def test_version_differs_busy_defers_launch_until_quiet() -> None:
    src = "curl … | bash -s -- --hermes-branch v9.9.9 --no-pair --stage"
    reg = FakeRegistrar(PluginVersion(current_version="9.9.9", source=src))
    # Busy on the first check (and its immediate launch attempt), quiet after.
    poller, rec = _poller(reg=reg, installed="1.0.0", is_busy=[True, True])

    # First tick: mismatch seen, but launch deferred (busy) — nothing run yet.
    await poller._check_once()
    assert rec["launched"] == []
    assert poller._pending_source == src

    # Next tick while still busy: does NOT re-poll the registrar, still deferred.
    await poller._check_once()
    assert len(reg.calls) == 1  # no second /plugin-version poll
    assert rec["launched"] == []

    # Next tick once quiet: launches the installer command.
    await poller._check_once()
    assert rec["launched"] == [src]
    assert poller._pending_source is None


async def test_launch_failure_re_evaluates_next_tick() -> None:
    src = "curl … | bash -s -- --hermes-branch v9.9.9 --no-pair --stage"
    reg = FakeRegistrar(PluginVersion(current_version="9.9.9", source=src))
    poller, rec = _poller(reg=reg, installed="1.0.0", launch_ok=False)
    await poller._check_once()
    # Launch attempted but failed → nothing pending; the next poll re-evaluates the
    # version from scratch (no stuck deferred state).
    assert rec["launched"] == [src]
    assert poller._pending_source is None


# ─── _spawn_installer: run source verbatim, detached ───────────────────────────


def test_spawn_installer_runs_source_verbatim_detached(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakePopen:
        def __init__(self, cmd: str, **kwargs: object) -> None:
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs

    monkeypatch.setattr(vp.subprocess, "Popen", FakePopen)
    cmd = "curl -fsSL https://…/install.sh | bash -s -- --hermes-branch v1.1.1 --no-pair --stage"
    assert vp._spawn_installer(cmd) is True
    # Run verbatim through a shell (the source has a pipe), in a new session so the
    # installer survives the gateway restart it triggers.
    assert captured["cmd"] == cmd
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["shell"] is True
    assert kwargs["start_new_session"] is True


def test_spawn_installer_returns_false_on_oserror(monkeypatch) -> None:
    def boom(*_a: object, **_k: object) -> object:
        raise OSError("cannot spawn")

    monkeypatch.setattr(vp.subprocess, "Popen", boom)
    assert vp._spawn_installer("anything") is False
