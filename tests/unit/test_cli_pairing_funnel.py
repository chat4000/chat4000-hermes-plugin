"""The pairing flow fires the full PostHog funnel.

Mocks the registrar + captures analytics.track so we assert the events without
network or a real PostHog client. (conftest disables real telemetry; we patch
track directly so capture works regardless.)
"""

from __future__ import annotations

import contextlib
from contextlib import asynccontextmanager

import pytest

import chat4000_hermes_plugin.analytics as analytics_mod
import chat4000_hermes_plugin.cli as cli
import chat4000_hermes_plugin.setup_flow as setup_flow
from chat4000_hermes_plugin.matrix.registrar_client import (
    RedeemResult,
    RegistrarError,
    UserEnsureResult,
    VersionVerdict,
)

# The plugin's one user (C.6.1) — the setup-ensured account every code binds to.
ENSURED_USER = "@u:hs"


class _FakeSetupRooms:
    """Room-manager stand-in for the C.6 step-3 short-lived session."""

    def __init__(self):
        self.space_id = None
        self.control_room_id = None
        self.created: list[str] = []
        self.invited: list[tuple[str, str]] = []

    async def discover(self):
        return None

    async def create_space(self, name="chat4000"):
        self.space_id = "!space:hs"
        self.created.append("space")
        return self.space_id

    async def create_control_room(self, name="Commands"):
        self.control_room_id = "!control:hs"
        self.created.append("control")
        return self.control_room_id

    async def invite_user(self, room_id, user_id):
        self.invited.append((room_id, user_id))


@pytest.fixture
def fake_room_session(monkeypatch):
    """Swap the real gateway-backed setup session for an offline fake."""
    rooms = _FakeSetupRooms()

    @asynccontextmanager
    async def _fake_open(creds):
        yield rooms

    monkeypatch.setattr(setup_flow, "_open_gateway_room_session", _fake_open)
    return rooms


class _OkReg:
    def __init__(self):
        self.version_calls: list = []
        self.register_calls: list = []
        self.user_ensure_calls: list = []

    async def version(self, *a, **k):
        self.version_calls.append((a, k))
        return VersionVerdict("ok", None, 1, None)

    async def self_onboard(self, code, device_name="x"):
        return RedeemResult("wss://gw/ws", "@plugin:hs", "DEV", "tok", "plug-id")

    async def user_ensure(self, plugin_id):
        self.user_ensure_calls.append(plugin_id)
        return UserEnsureResult(user_id=ENSURED_USER, created=len(self.user_ensure_calls) == 1)

    async def register(self, code, **k):
        self.register_calls.append((code, k))
        return {"ok": True, "expires_at": 1234567890123}

    async def poll_until_complete(self, code, **k):
        # FLW2: completed /pair/status payload, with the phone's client_id.
        return {
            "status": "completed",
            "user_id": ENSURED_USER,
            "client_id": "phone-cid-1",
            "redeems": [
                {"device_id": "D1", "client_id": "phone-cid-1", "redeemed_at": 1234567890123}
            ],
            "redeemed_count": 1,
        }


class _ExpiredReg(_OkReg):
    async def poll_until_complete(self, code, **k):
        return None


class _Down502Reg:
    async def version(self, *a, **k):
        raise RegistrarError(502, "M_HOMESERVER_UNAVAILABLE", "registrar down")

    async def self_onboard(self, code, device_name="x"):
        raise RegistrarError(502, "M_HOMESERVER_UNAVAILABLE", "registrar down")


def _capture(monkeypatch):
    events: list[tuple[str, dict | None]] = []
    monkeypatch.setattr(analytics_mod, "track", lambda e, p=None: events.append((e, p)))
    monkeypatch.setattr(analytics_mod, "flush", lambda *a, **k: None)
    return events


def _names(events):
    return [e for e, _ in events]


async def test_full_funnel_on_success(monkeypatch, fake_room_session):
    monkeypatch.setenv("CHAT4000_SERVICE_TOKEN", "tok")
    reg = _OkReg()
    monkeypatch.setattr(cli, "_registrar", lambda: reg)
    registered: list[str] = []
    monkeypatch.setattr(analytics_mod, "register_paired_client_id", registered.append)
    events = _capture(monkeypatch)
    await cli._run_pair("default")
    # PL3: telemetry is disabled in tests → the X-Client-Id value is None
    # (the id never rides any wire); the posthog_id body field is gone.
    assert reg.version_calls == [
        (("@chat4000/hermes-plugin", "1.1.0", "production"), {"client_id": None})
    ]
    names = _names(events)
    for expected in (
        "pairing_started",
        "plugin_onboarded",
        "pairing_code_registered",
        "pairing_completed",
    ):
        assert expected in names, f"{expected} missing from {names}"
    # FLW3/FLW4: the join event carries the phone's id + super property.
    completed_props = dict(events[names.index("pairing_completed")][1] or {})
    assert completed_props["paired_client_id"] == "phone-cid-1"
    assert registered == ["phone-cid-1"]


async def test_expired_fires_pairing_expired(monkeypatch, fake_room_session):
    monkeypatch.setenv("CHAT4000_SERVICE_TOKEN", "tok")
    monkeypatch.setattr(cli, "_registrar", lambda: _ExpiredReg())
    events = _capture(monkeypatch)
    await cli._run_pair("default")
    assert "pairing_expired" in _names(events)
    assert "pairing_completed" not in _names(events)


async def test_pair_registers_bound_code_with_default_flags(monkeypatch, fake_room_session):
    """C.1: the code rides with plugin_id (the registrar binds it to the plugin's
    one user); the default case sends NO ttl/reusable overrides."""
    monkeypatch.setenv("CHAT4000_SERVICE_TOKEN", "tok")
    reg = _OkReg()
    monkeypatch.setattr(cli, "_registrar", lambda: reg)
    _capture(monkeypatch)
    await cli._run_pair("default")
    assert len(reg.register_calls) == 1
    code, kwargs = reg.register_calls[0]
    assert len(code) == 6 and code.isdigit()
    assert kwargs["kind"] == "user"
    assert kwargs["plugin_id"] == "plug-id"
    assert kwargs["ttl_seconds"] is None
    assert kwargs["reusable"] is False


async def test_pair_passes_ttl_and_reusable_through(monkeypatch, fake_room_session):
    monkeypatch.setenv("CHAT4000_SERVICE_TOKEN", "tok")
    reg = _OkReg()
    monkeypatch.setattr(cli, "_registrar", lambda: reg)
    _capture(monkeypatch)
    await cli._run_pair("default", ttl_seconds=63072000, reusable=True)
    _, kwargs = reg.register_calls[0]
    assert kwargs["ttl_seconds"] == 63072000
    assert kwargs["reusable"] is True


async def test_pair_records_outstanding_code_for_the_resident_listener(
    monkeypatch, fake_room_session
):
    """C.4: the registered code is persisted so the gateway-resident completion
    listener owns it for its whole lifetime (the CLI watcher is feedback only)."""
    from chat4000_hermes_plugin.matrix.pair_codes_store import load_pending_codes

    monkeypatch.setenv("CHAT4000_SERVICE_TOKEN", "tok")
    reg = _OkReg()
    monkeypatch.setattr(cli, "_registrar", lambda: reg)
    _capture(monkeypatch)
    await cli._run_pair("default", reusable=True)
    records = load_pending_codes("default")
    assert len(records) == 1
    code, _ = reg.register_calls[0]
    assert records[0].code == code
    assert records[0].reusable is True
    assert records[0].expires_at_ms == 1234567890123
    assert records[0].pair_id is None


async def test_reusable_no_redeem_is_not_tracked_as_expired(monkeypatch, fake_room_session):
    """A reusable code never settles (C.3): the watcher timing out without a
    redeem is NOT an expiry — no pairing_expired event."""
    monkeypatch.setenv("CHAT4000_SERVICE_TOKEN", "tok")
    monkeypatch.setattr(cli, "_registrar", lambda: _ExpiredReg())
    events = _capture(monkeypatch)
    await cli._run_pair("default", reusable=True)
    assert "pairing_expired" not in _names(events)
    assert "pairing_completed" not in _names(events)


async def test_setup_runs_user_ensure_and_invites_before_the_code(monkeypatch, fake_room_session):
    """C.6: setup creates the user + space + control room + invites BEFORE the
    pairing code exists, so pairing is purely a device operation."""
    monkeypatch.setenv("CHAT4000_SERVICE_TOKEN", "tok")
    reg = _OkReg()
    monkeypatch.setattr(cli, "_registrar", lambda: reg)
    _capture(monkeypatch)
    await cli._run_pair("default")
    assert reg.user_ensure_calls == ["plug-id"]
    assert fake_room_session.created == ["space", "control"]
    assert fake_room_session.invited == [
        ("!space:hs", ENSURED_USER),
        ("!control:hs", ENSURED_USER),
    ]


def test_handle_cli_error_tracks_pairing_failed_and_exits_nonzero(monkeypatch):
    import pytest

    events = _capture(monkeypatch)
    with pytest.raises(SystemExit) as ei:
        cli._handle_cli_error(RegistrarError(502, "M_HOMESERVER_UNAVAILABLE", "down"))
    assert ei.value.code == 1  # non-zero so the wizard knows it failed
    assert "pairing_failed" in _names(events)


async def test_registrar_down_fires_version_check_failed(monkeypatch):
    # version() 502 is non-fatal (skipped); then self_onboard 502 raises → caller
    # handles + tracks pairing_failed. Here we assert the version_check_failed event.
    monkeypatch.setenv("CHAT4000_SERVICE_TOKEN", "tok")
    monkeypatch.setattr(cli, "_registrar", lambda: _Down502Reg())
    events = _capture(monkeypatch)
    with contextlib.suppress(RegistrarError):
        await cli._run_pair("default")
    assert "pairing_started" in _names(events)
    assert "version_check_failed" in _names(events)
