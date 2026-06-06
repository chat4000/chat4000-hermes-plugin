"""`chat4000 uninstall` — config-disable + full state-dir removal.

These exercise the helpers directly (no click runner): the uninstall must reverse
the install footprint — drop 'chat4000' from plugins.enabled and delete the whole
plugin state dir — while staying idempotent and best-effort.

The config-disable needs PyYAML (the plugin imports it lazily and no-ops without
it, mirroring the Hermes host where yaml is always present); those tests
`importorskip`. State-dir removal needs no yaml and is tested unconditionally.
"""

from __future__ import annotations

import pytest

import chat4000_hermes_plugin.analytics as analytics_mod
import chat4000_hermes_plugin.cli as cli


def _silence_analytics(monkeypatch):
    monkeypatch.setattr(analytics_mod, "track", lambda *a, **k: None)
    monkeypatch.setattr(analytics_mod, "flush", lambda *a, **k: None)


def test_disable_removes_chat4000_from_enabled(monkeypatch, tmp_path):
    yaml = pytest.importorskip("yaml")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"plugins": {"enabled": ["chat4000", "telegram"]}}))

    cli._disable_plugin_in_hermes_config()

    data = yaml.safe_load(cfg.read_text())
    assert data["plugins"]["enabled"] == ["telegram"]  # only chat4000 dropped


def test_disable_is_idempotent_noop_when_absent(monkeypatch, tmp_path):
    yaml = pytest.importorskip("yaml")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"plugins": {"enabled": ["telegram"]}}))

    cli._disable_plugin_in_hermes_config()  # chat4000 not present → unchanged

    assert yaml.safe_load(cfg.read_text())["plugins"]["enabled"] == ["telegram"]


def test_disable_noop_when_no_config(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))  # no config.yaml written
    cli._disable_plugin_in_hermes_config()  # must not raise / create the file
    assert not (tmp_path / "config.yaml").exists()


def test_uninstall_removes_state_dir(monkeypatch, tmp_path):
    _silence_analytics(monkeypatch)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_STATE_DIR", str(tmp_path))
    # Stand up plugin state across two accounts, as a real install would.
    plugin_dir = tmp_path / "plugins" / "chat4000"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "matrix-creds-default.json").write_text("{}")
    (plugin_dir / "known-users-work.json").write_text("{}")

    cli._run_uninstall()

    assert not plugin_dir.exists()  # entire state dir gone (all accounts)


def test_uninstall_disables_config_when_yaml_present(monkeypatch, tmp_path):
    yaml = pytest.importorskip("yaml")
    _silence_analytics(monkeypatch)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_STATE_DIR", str(tmp_path))
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"plugins": {"enabled": ["chat4000"]}}))

    cli._run_uninstall()

    assert yaml.safe_load(cfg.read_text())["plugins"]["enabled"] == []


def test_uninstall_is_safe_when_nothing_installed(monkeypatch, tmp_path):
    _silence_analytics(monkeypatch)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_STATE_DIR", str(tmp_path))
    cli._run_uninstall()  # no state dir, no config → must not raise
    assert not (tmp_path / "plugins" / "chat4000").exists()
