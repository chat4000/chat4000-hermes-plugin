"""Plugin state-dir resolution (the v2 path helpers)."""

from __future__ import annotations

from pathlib import Path

from chat4000_hermes_plugin.key_store import (
    resolve_chat4000_plugin_dir,
    resolve_hermes_home,
    resolve_hermes_state_dir,
)


def test_hermes_home_uses_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HERMES_STATE_DIR", raising=False)
    assert resolve_hermes_home() == tmp_path / "home"


def test_state_dir_overrides_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_STATE_DIR", str(tmp_path / "custom"))
    assert resolve_hermes_state_dir() == tmp_path / "custom"


def test_plugin_dir_under_state_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_STATE_DIR", str(tmp_path / "s"))
    plugin = resolve_chat4000_plugin_dir()
    assert plugin == tmp_path / "s" / "plugins" / "chat4000"
    assert Path(plugin).name == "chat4000"
