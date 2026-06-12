"""Plugin setup (protocol C.6) — one user per plugin, idempotent across re-runs.

Fakes the registrar and the short-lived room session; asserts the C.6 order
(onboard → /user/ensure → rooms + invites), idempotency (re-running setup never
creates a second user / duplicate rooms / failing invites), and the
single-crypto-owner rule (the setup path never touches the OlmMachine — the
fake session has no crypto surface at all, so any crypto call would explode).
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from chat4000_hermes_plugin import setup_flow
from chat4000_hermes_plugin.matrix.creds_store import BotCreds, save_bot_creds
from chat4000_hermes_plugin.matrix.registrar_client import RedeemResult, UserEnsureResult
from chat4000_hermes_plugin.matrix.users_store import load_known_users

PLUGIN_ID = "11111111-2222-3333-4444-555555555555"
ENSURED_USER = "@u_one:hs"


class FakeRegistrar:
    """user_ensure is idempotent per plugin_id — created only on the first call."""

    def __init__(self):
        self.user_ensure_calls: list[str] = []
        self.onboard_calls = 0

    async def self_onboard(self, code, device_name="hermes-plugin"):
        self.onboard_calls += 1
        return RedeemResult("wss://gw/ws", "@plugin_x:hs", "DEV", "tok", PLUGIN_ID)

    async def user_ensure(self, plugin_id):
        self.user_ensure_calls.append(plugin_id)
        return UserEnsureResult(user_id=ENSURED_USER, created=len(self.user_ensure_calls) == 1)


class FakeRooms:
    """Discoverable room state shared across setup re-runs (like the homeserver)."""

    def __init__(self):
        self.space_id = None
        self.control_room_id = None
        self.create_calls: list[str] = []
        self.invites: list[tuple[str, str]] = []
        self.discover_calls = 0
        # The "homeserver's" persistent truth, fed back by discover().
        self.existing_space = None
        self.existing_control = None

    async def discover(self):
        self.discover_calls += 1
        self.space_id = self.existing_space
        self.control_room_id = self.existing_control

    async def create_space(self, name="chat4000"):
        self.create_calls.append("space")
        self.space_id = self.existing_space = "!space:hs"
        return self.space_id

    async def create_control_room(self, name="Commands"):
        self.create_calls.append("control")
        self.control_room_id = self.existing_control = "!control:hs"
        return self.control_room_id

    async def invite_user(self, room_id, user_id):
        # Idempotent like the real one (re-inviting is benign) — never raises.
        self.invites.append((room_id, user_id))


@pytest.fixture
def rooms(monkeypatch):
    r = FakeRooms()

    @asynccontextmanager
    async def _open(creds):
        # Fresh manager per session in real life; the fake keeps homeserver
        # state in `existing_*` so a re-run discovers, not recreates.
        r.space_id = None
        r.control_room_id = None
        yield r

    monkeypatch.setattr(setup_flow, "_open_gateway_room_session", _open)
    return r


async def test_setup_first_run_creates_everything_in_order(rooms):
    reg = FakeRegistrar()
    outcome = await setup_flow.ensure_setup("default", registrar=reg)
    assert outcome is not None
    assert reg.onboard_calls == 1
    assert reg.user_ensure_calls == [PLUGIN_ID]
    assert outcome.user_id == ENSURED_USER
    assert outcome.user_created is True
    assert outcome.space_id == "!space:hs"
    assert outcome.control_room_id == "!control:hs"
    assert rooms.create_calls == ["space", "control"]
    # The user is invited to BOTH rooms at setup — before any device pairs.
    assert rooms.invites == [("!space:hs", ENSURED_USER), ("!control:hs", ENSURED_USER)]
    # The one user is durably recorded for the gateway's member/key-share floor.
    assert load_known_users("default") == [ENSURED_USER]


async def test_setup_is_idempotent_across_reruns(rooms):
    reg = FakeRegistrar()
    first = await setup_flow.ensure_setup("default", registrar=reg)
    second = await setup_flow.ensure_setup("default", registrar=reg)
    assert first is not None and second is not None
    # Same bot identity (creds reused — onboarded exactly once)…
    assert reg.onboard_calls == 1
    # …same ONE user (idempotent /user/ensure, created=False on the repeat)…
    assert reg.user_ensure_calls == [PLUGIN_ID, PLUGIN_ID]
    assert second.user_id == ENSURED_USER
    assert second.user_created is False
    # …and no duplicate rooms: the re-run discovers, it does not recreate.
    assert rooms.create_calls == ["space", "control"]
    assert second.space_id == "!space:hs"
    assert second.control_room_id == "!control:hs"
    # Re-inviting is benign and attempted again (no failure on existing invites).
    assert rooms.invites.count(("!space:hs", ENSURED_USER)) == 2
    # The known-users store stays a single entry — one user per plugin (B).
    assert load_known_users("default") == [ENSURED_USER]


async def test_setup_requires_plugin_id_for_user_ensure(rooms):
    # Pre-redesign creds without plugin_id cannot run /user/ensure (C.6.1).
    save_bot_creds(
        BotCreds(
            user_id="@plugin_x:hs",
            device_id="DEV",
            access_token="tok",  # noqa: S106  # test fixture, not a secret
            gateway_url="wss://gw/ws",
            plugin_id=None,
        ),
        "default",
    )
    with pytest.raises(RuntimeError, match="plugin_id"):
        await setup_flow.ensure_setup("default", registrar=FakeRegistrar())


async def test_setup_surfaces_registrar_failure(rooms):
    """A dead registrar during self-onboard surfaces as the RegistrarError the
    CLI's error boundary classifies — setup neither swallows nor half-runs."""
    from chat4000_hermes_plugin.matrix.registrar_client import RegistrarError

    class DownReg(FakeRegistrar):
        async def self_onboard(self, code, device_name="hermes-plugin"):
            raise RegistrarError(503, "M_HOMESERVER_UNAVAILABLE", "down")

    with pytest.raises(RegistrarError):
        await setup_flow.ensure_setup("default", registrar=DownReg())
    assert rooms.create_calls == []  # nothing past step 1 ran
    assert load_known_users("default") == []
