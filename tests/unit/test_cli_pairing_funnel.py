"""The CLI pair flow's analytics surface (plan v5 DEC3 + PL4).

DEC3: the plugin emits NO pair-funnel events of its own — the funnel
(pairing_started / pairing_expired / version_checked / pairing_failed) is
observed registrar-side. The ONE plugin event here is PL4 `pairing_completed`,
once per redeemed device, with the canonical props {paired_client_id?,
reusable, redeem_index?}, deduped against the gateway-resident listener via
the pending-codes store's `redeemed_count_seen` check-and-set.

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
    PluginBirth,
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

    async def create_session_room_and_invite(self, members, title="New chat", agent_id="main"):
        # C.6 step-3 starter chat room: one session room + invites.
        self.created.append("session")
        room_id = "!starter:hs"
        for uid in members:
            self.invited.append((room_id, uid))
        return room_id

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
        self.user_ensure_calls = 0
        self.bot_token = None

    def set_bot_token(self, token):
        self.bot_token = token

    async def version(self, *a, **k):
        self.version_calls.append((a, k))
        return VersionVerdict("ok", None, 1, None)

    async def self_onboard(self, device_name="x"):
        self.bot_token = "tok"  # noqa: S105  # test fixture, not a secret
        return PluginBirth("@plugin:hs", "tok", "DEV", "wss://gw/ws")

    async def user_ensure(self):
        self.user_ensure_calls += 1
        return UserEnsureResult(user_id=ENSURED_USER, created=self.user_ensure_calls == 1)

    async def create_code(self, code, **k):
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
    def set_bot_token(self, token):
        pass

    async def version(self, *a, **k):
        raise RegistrarError(502, "M_HOMESERVER_UNAVAILABLE", "registrar down")

    async def self_onboard(self, device_name="x"):
        raise RegistrarError(502, "M_HOMESERVER_UNAVAILABLE", "registrar down")


def _capture(monkeypatch):
    events: list[tuple[str, dict | None]] = []
    monkeypatch.setattr(analytics_mod, "track", lambda e, p=None: events.append((e, p)))
    monkeypatch.setattr(analytics_mod, "flush", lambda *a, **k: None)
    return events


def _names(events):
    return [e for e, _ in events]


async def test_success_emits_only_pairing_completed_with_pl4_props(monkeypatch, fake_room_session):
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
    # DEC3: pairing_completed is the ONLY plugin event in the whole flow.
    assert _names(events) == ["pairing_completed"]
    # PL4 canonical props + FLW3/FLW4 join.
    completed_props = dict(events[0][1] or {})
    assert completed_props["paired_client_id"] == "phone-cid-1"
    assert completed_props["reusable"] is False
    assert completed_props["redeem_index"] == 1
    assert registered == ["phone-cid-1"]


async def test_success_advances_the_dedupe_count_in_the_store(monkeypatch, fake_room_session):
    """PL4 dedupe: the CLI watcher records the redeem it reported, so the
    resident listener (which only processes entries beyond
    `redeemed_count_seen`) never double-fires for the same device."""
    from chat4000_hermes_plugin.matrix.pair_codes_store import load_pending_codes

    monkeypatch.setenv("CHAT4000_SERVICE_TOKEN", "tok")
    reg = _OkReg()
    monkeypatch.setattr(cli, "_registrar", lambda: reg)
    _capture(monkeypatch)
    await cli._run_pair("default")
    records = load_pending_codes("default")
    assert len(records) == 1
    assert records[0].redeemed_count_seen == 1


async def test_watcher_skips_redeems_the_listener_already_recorded(monkeypatch, tmp_path):
    """The other direction of the check-and-set: when the resident listener
    won the race (`redeemed_count_seen` already covers the redeem), the CLI
    watcher emits nothing."""
    from chat4000_hermes_plugin.matrix.pair_codes_store import PendingCode, add_pending_code

    events = _capture(monkeypatch)
    add_pending_code(
        PendingCode(code="111222", expires_at_ms=0, reusable=False, redeemed_count_seen=1),
        "default",
    )
    cli._track_watcher_redeems(
        {
            "status": "completed",
            "user_id": ENSURED_USER,
            "client_id": "phone-cid-1",
            "redeems": [{"device_id": "D1", "client_id": "phone-cid-1", "redeemed_at": 1}],
            "redeemed_count": 1,
        },
        code="111222",
        account="default",
        reusable=False,
    )
    assert events == []


async def test_expired_emits_no_plugin_events(monkeypatch, fake_room_session):
    """DEC3: expiry is the registrar's pairing_expired row, not a plugin event."""
    monkeypatch.setenv("CHAT4000_SERVICE_TOKEN", "tok")
    monkeypatch.setattr(cli, "_registrar", lambda: _ExpiredReg())
    events = _capture(monkeypatch)
    await cli._run_pair("default")
    assert events == []


async def test_pair_mints_bound_code_with_default_flags(monkeypatch, fake_room_session):
    """C.3.1: the code is minted via POST /codes — bound implicitly to the
    plugin's one DERIVED user by the bot token (no kind/plugin_id/user_id); the
    default case sends NO ttl/reusable overrides."""
    monkeypatch.setenv("CHAT4000_SERVICE_TOKEN", "tok")
    reg = _OkReg()
    monkeypatch.setattr(cli, "_registrar", lambda: reg)
    _capture(monkeypatch)
    await cli._run_pair("default")
    assert len(reg.register_calls) == 1
    code, kwargs = reg.register_calls[0]
    assert len(code) == 6 and code.isdigit()
    assert "kind" not in kwargs
    assert "plugin_id" not in kwargs
    assert "user_id" not in kwargs
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


async def test_reusable_completion_carries_reusable_prop(monkeypatch, fake_room_session):
    """PL4: a reusable code's redeem reports reusable=True on the event."""
    monkeypatch.setenv("CHAT4000_SERVICE_TOKEN", "tok")
    reg = _OkReg()
    monkeypatch.setattr(cli, "_registrar", lambda: reg)
    monkeypatch.setattr(analytics_mod, "register_paired_client_id", lambda v: None)
    events = _capture(monkeypatch)
    await cli._run_pair("default", reusable=True)
    assert _names(events) == ["pairing_completed"]
    props = dict(events[0][1] or {})
    assert props["reusable"] is True
    assert props["redeem_index"] == 1


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


async def test_reusable_no_redeem_emits_nothing(monkeypatch, fake_room_session):
    """A reusable code never settles (C.3): the watcher timing out without a
    redeem is NOT an expiry — and per DEC3 no plugin event either way."""
    monkeypatch.setenv("CHAT4000_SERVICE_TOKEN", "tok")
    monkeypatch.setattr(cli, "_registrar", lambda: _ExpiredReg())
    events = _capture(monkeypatch)
    await cli._run_pair("default", reusable=True)
    assert events == []


async def test_setup_runs_user_ensure_and_invites_before_the_code(monkeypatch, fake_room_session):
    """C.6: setup creates the user + space + control room + starter chat room +
    invites BEFORE the pairing code exists, so pairing is purely a device
    operation and a redeemed device opens to a usable chat (E "The starter
    chat room")."""
    monkeypatch.setenv("CHAT4000_SERVICE_TOKEN", "tok")
    reg = _OkReg()
    monkeypatch.setattr(cli, "_registrar", lambda: reg)
    _capture(monkeypatch)
    await cli._run_pair("default")
    assert reg.user_ensure_calls == 1
    assert fake_room_session.created == ["space", "control", "session"]
    assert fake_room_session.invited == [
        ("!space:hs", ENSURED_USER),
        ("!control:hs", ENSURED_USER),
        ("!starter:hs", ENSURED_USER),
    ]


def test_handle_cli_error_exits_nonzero_without_plugin_events(monkeypatch):
    """DEC3: registrar failures are the registrar's to observe — the CLI prints
    + exits non-zero but emits nothing."""
    events = _capture(monkeypatch)
    with pytest.raises(SystemExit) as ei:
        cli._handle_cli_error(RegistrarError(502, "M_HOMESERVER_UNAVAILABLE", "down"))
    assert ei.value.code == 1  # non-zero so callers know it failed
    assert events == []


async def test_registrar_down_emits_no_plugin_events(monkeypatch):
    # version() 502 is non-fatal (skipped); then self_onboard 502 raises → the
    # caller handles it. DEC3: neither failure produces a plugin event.
    monkeypatch.setenv("CHAT4000_SERVICE_TOKEN", "tok")
    monkeypatch.setattr(cli, "_registrar", lambda: _Down502Reg())
    events = _capture(monkeypatch)
    with contextlib.suppress(RegistrarError):
        await cli._run_pair("default")
    assert events == []
