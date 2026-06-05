"""The pairing flow fires the full PostHog funnel.

Mocks the registrar + captures analytics.track so we assert the events without
network or a real PostHog client. (conftest disables real telemetry; we patch
track directly so capture works regardless.)
"""

from __future__ import annotations

import chat4000_hermes_plugin.analytics as analytics_mod
import chat4000_hermes_plugin.cli as cli
from chat4000_hermes_plugin.matrix.registrar_client import (
    RedeemResult,
    RegistrarError,
    VersionVerdict,
)


class _OkReg:
    async def version(self, *a, **k):
        return VersionVerdict("ok", None, 1, None)

    async def self_onboard(self, code, device_name="x"):
        return RedeemResult("wss://gw/ws", "@plugin:hs", "DEV", "tok", "plug-id")

    async def register(self, code, **k):
        return {"ok": True}

    async def poll_until_complete(self, code, **k):
        return "@u:hs"


class _ExpiredReg(_OkReg):
    async def poll_until_complete(self, code, **k):
        return None


class _Down502Reg:
    async def version(self, *a, **k):
        raise RegistrarError(502, "M_HOMESERVER_UNAVAILABLE", "registrar down")

    async def self_onboard(self, code, device_name="x"):
        raise RegistrarError(502, "M_HOMESERVER_UNAVAILABLE", "registrar down")


def _capture(monkeypatch):
    events: list[str] = []
    monkeypatch.setattr(analytics_mod, "track", lambda e, p=None: events.append(e))
    monkeypatch.setattr(analytics_mod, "flush", lambda *a, **k: None)
    return events


async def test_full_funnel_on_success(monkeypatch):
    monkeypatch.setenv("CHAT4000_SERVICE_TOKEN", "tok")
    monkeypatch.setattr(cli, "_registrar", lambda: _OkReg())
    events = _capture(monkeypatch)
    await cli._run_pair("default")
    for expected in (
        "pairing_started",
        "plugin_onboarded",
        "pairing_code_registered",
        "pairing_completed",
    ):
        assert expected in events, f"{expected} missing from {events}"


async def test_expired_fires_pairing_expired(monkeypatch):
    monkeypatch.setenv("CHAT4000_SERVICE_TOKEN", "tok")
    monkeypatch.setattr(cli, "_registrar", lambda: _ExpiredReg())
    events = _capture(monkeypatch)
    await cli._run_pair("default")
    assert "pairing_expired" in events
    assert "pairing_completed" not in events


def test_handle_cli_error_tracks_pairing_failed_and_exits_nonzero(monkeypatch):
    import pytest

    events = _capture(monkeypatch)
    with pytest.raises(SystemExit) as ei:
        cli._handle_cli_error(RegistrarError(502, "M_HOMESERVER_UNAVAILABLE", "down"))
    assert ei.value.code == 1  # non-zero so the wizard knows it failed
    assert "pairing_failed" in events


async def test_registrar_down_fires_version_check_failed(monkeypatch):
    # version() 502 is non-fatal (skipped); then self_onboard 502 raises → caller
    # handles + tracks pairing_failed. Here we assert the version_check_failed event.
    monkeypatch.setenv("CHAT4000_SERVICE_TOKEN", "tok")
    monkeypatch.setattr(cli, "_registrar", lambda: _Down502Reg())
    events = _capture(monkeypatch)
    try:
        await cli._run_pair("default")
    except RegistrarError:
        pass  # propagates to _handle_cli_error in the real CLI
    assert "pairing_started" in events
    assert "version_check_failed" in events
