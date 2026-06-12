"""Analytics plan v5 machine identity: IDN7/IDN8/IDN9, PL1-PL5, FLW3-FLW4.

Covers the two-id design (env_id property + agent_install_id distinct id),
the container_rebuilt classifier, the boot events, the paired_client_id
super-property store, and the PL3 X-Client-Id header on registrar calls.
"""

from __future__ import annotations

import io
import json
import os
import stat
import urllib.request
from pathlib import Path

import chat4000_hermes_plugin.analytics as analytics_mod
import chat4000_hermes_plugin.machine_ids as machine_ids
import chat4000_hermes_plugin.matrix.commands as matrix_commands
import chat4000_hermes_plugin.telemetry as telemetry_mod
from chat4000_hermes_plugin.matrix.commands import CommandHandler
from chat4000_hermes_plugin.matrix.registrar_client import RegistrarClient

from .test_matrix_commands import FakeRegistrar, FakeSession


def _fresh_ids(monkeypatch, tmp_path):
    """Point the id/telemetry files into tmp (the telemetry module computed its
    paths at import time, before conftest's HOME override) + clear the cache."""
    monkeypatch.setattr(machine_ids, "_cached_agent_install_id", None)
    monkeypatch.setattr(telemetry_mod, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(telemetry_mod, "INSTALL_ID_PATH", tmp_path / "config" / "install-id")
    monkeypatch.setattr(
        telemetry_mod, "TELEMETRY_ENABLED_PATH", tmp_path / "config" / "telemetry-enabled"
    )


# ─── IDN8: the durable agent_install_id ────────────────────────────────────


def test_agent_install_id_minted_at_hermes_home_root(monkeypatch, tmp_path):
    _fresh_ids(monkeypatch, tmp_path)
    first = machine_ids.read_or_mint_agent_install_id()
    path = machine_ids.agent_install_id_path()
    assert path == Path(os.environ["HERMES_HOME"]) / "chat4000-install-id"
    assert path.read_text() == first + "\n"  # trailing newline, env-id format
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # Stable: cached and re-read both return the same id.
    assert machine_ids.read_or_mint_agent_install_id() == first
    monkeypatch.setattr(machine_ids, "_cached_agent_install_id", None)
    assert machine_ids.read_or_mint_agent_install_id() == first


def test_agent_install_id_reads_preexisting_file(monkeypatch, tmp_path):
    _fresh_ids(monkeypatch, tmp_path)
    path = machine_ids.agent_install_id_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("installer-minted-id\n")  # the installer mints the SAME file
    assert machine_ids.read_or_mint_agent_install_id() == "installer-minted-id"


# ─── IDN9: the container_rebuilt classifier ────────────────────────────────


def test_rebuild_detected_when_agent_id_survives_but_env_id_fresh(monkeypatch, tmp_path):
    _fresh_ids(monkeypatch, tmp_path)
    machine_ids.read_or_mint_agent_install_id()  # durable id exists
    assert machine_ids.detect_container_rebuilt() is True  # env file absent


def test_no_rebuild_when_both_ids_present_or_both_fresh(monkeypatch, tmp_path):
    _fresh_ids(monkeypatch, tmp_path)
    assert machine_ids.detect_container_rebuilt() is False  # both fresh = new machine
    machine_ids.read_or_mint_agent_install_id()
    env = telemetry_mod.INSTALL_ID_PATH
    env.parent.mkdir(parents=True, exist_ok=True)
    env.write_text("env-id\n")
    assert machine_ids.detect_container_rebuilt() is False  # both present = normal boot


# ─── PL1 + PL5: boot events ────────────────────────────────────────────────


def test_boot_emits_rebuilt_then_started(monkeypatch):
    events: list[tuple[str, dict | None]] = []
    monkeypatch.setattr(analytics_mod, "track", lambda e, p=None: events.append((e, p)))
    analytics_mod.emit_plugin_boot_analytics(container_rebuilt=True)
    assert [e for e, _ in events] == ["container_rebuilt", "plugin_started"]
    started = events[1][1] or {}
    assert started["agent_kind"] == "hermes"
    assert "agent_version" in started


def test_boot_without_rebuild_emits_only_started(monkeypatch):
    events: list[str] = []
    monkeypatch.setattr(analytics_mod, "track", lambda e, p=None: events.append(e))
    analytics_mod.emit_plugin_boot_analytics(container_rebuilt=False)
    assert events == ["plugin_started"]


# ─── IDN7 + FLW4: universal properties ─────────────────────────────────────


def test_universal_properties_carry_env_id_and_latest_paired_client_id(monkeypatch, tmp_path):
    _fresh_ids(monkeypatch, tmp_path)
    props = analytics_mod._universal_properties()
    assert props["env_id"]  # IDN7 rides as a property
    assert "paired_client_id" not in props  # nothing paired yet
    analytics_mod.register_paired_client_id("phone-A")
    analytics_mod.register_paired_client_id("phone-B")  # latest wins
    assert analytics_mod._universal_properties()["paired_client_id"] == "phone-B"


def test_machine_client_id_is_none_when_telemetry_disabled(monkeypatch, tmp_path):
    _fresh_ids(monkeypatch, tmp_path)
    # conftest sets CHAT4000_TELEMETRY_DISABLED=1 for every test.
    assert analytics_mod.machine_client_id() is None
    monkeypatch.delenv("CHAT4000_TELEMETRY_DISABLED")
    assert analytics_mod.machine_client_id() == machine_ids.read_or_mint_agent_install_id()


# ─── PL3: X-Client-Id header, posthog_id body field gone ───────────────────


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


async def test_registrar_sends_header_only_no_posthog_body(monkeypatch):
    captured: list[urllib.request.Request] = []

    def fake_urlopen(req, timeout=0.0):
        captured.append(req)
        return _FakeResponse(b'{"current_version": "9", "source": "https://x/i.sh"}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    reg = RegistrarClient("https://registrar.test", "svc")
    await reg.version("@app", "1.0", "production", client_id="machine-id-1")
    await reg.plugin_version("@app", client_id="machine-id-1")
    await reg.version("@app", "1.0", "production", client_id=None)  # telemetry off
    assert captured[0].get_header("X-client-id") == "machine-id-1"
    assert captured[1].get_header("X-client-id") == "machine-id-1"
    assert captured[2].get_header("X-client-id") is None
    for req in captured:
        assert b"posthog_id" not in (req.data or b"")
    assert json.loads(captured[1].data)["app_id"] == "@app"


# ─── PL2 + PL4: command-driven upgrade + device-pair join ──────────────────


async def test_plugin_update_emits_plugin_upgrading(monkeypatch):
    events: list[tuple[str, dict | None]] = []
    monkeypatch.setattr(analytics_mod, "track", lambda e, p=None: events.append((e, p)))
    monkeypatch.setattr(analytics_mod, "flush", lambda *a, **k: None)
    s = FakeSession()

    async def install(source):
        return None

    await CommandHandler(
        s,
        version="2.1.0",
        registrar=FakeRegistrar(),
        client_id="cid",
        installer_runner=install,
        restart_scheduler=lambda: None,
    ).handle("!control:hs", "plugin.update", {})
    upgrading = dict(events[[e for e, _ in events].index("plugin_upgrading")][1] or {})
    assert upgrading == {"from_version": "2.1.0", "to_version": "2.2.0", "trigger": "command"}


async def test_device_pair_completion_emits_join_event(monkeypatch):
    monkeypatch.setattr(matrix_commands, "_gen_device_pair_code", lambda: "428913")
    monkeypatch.setattr(matrix_commands, "_gen_pair_id", lambda: "p_7af3c1")
    monkeypatch.setattr(matrix_commands, "PAIR_STATUS_POLL_INTERVAL_S", 0.0)
    events: list[tuple[str, dict | None]] = []
    registered: list[str] = []
    monkeypatch.setattr(analytics_mod, "track", lambda e, p=None: events.append((e, p)))
    monkeypatch.setattr(analytics_mod, "register_paired_client_id", registered.append)
    s = FakeSession()
    reg = FakeRegistrar(
        statuses=[{"status": "completed", "user_id": "@u:hs", "client_id": "phone-cid-9"}]
    )
    handler = CommandHandler(s, registrar=reg, client_id="cid")
    await handler.handle("!control:hs", "device.pair_start", {}, sender="@u:hs")
    pending = handler._pairings.get("p_7af3c1")
    if pending is not None:
        await pending.task
    completed = [p for e, p in events if e == "pairing_completed"]
    assert completed and (completed[0] or {})["paired_client_id"] == "phone-cid-9"
    # PL4 canonical props: device.pair_start codes are single-use, and the
    # old-registrar completed shape (no redeems[]) counts as redeem 1.
    assert (completed[0] or {})["reusable"] is False
    assert (completed[0] or {})["redeem_index"] == 1
    assert registered == ["phone-cid-9"]
