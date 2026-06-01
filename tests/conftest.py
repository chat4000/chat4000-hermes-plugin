"""Shared pytest fixtures.

The plugin reads/writes state under `~/.hermes/plugins/chat4000/...`. For
test isolation we override `HERMES_HOME` / `HERMES_STATE_DIR` to a fresh
`tmp_path` for every test, and also override `$HOME` so the telemetry
config (which lives under `~/.config/chat4000/`) doesn't bleed across
tests or pollute the developer's real config.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Put the project root on sys.path so `import chat4000_hermes_plugin.<module>`
# works without an editable install. The package lives at the repo root.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Redirect every plugin storage path into the per-test tmp_path.

    Autouse so no test can accidentally write to the developer's real
    ~/.hermes or ~/.config. Tests that need stable cached state can still
    write into tmp_path freely."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setenv("HERMES_STATE_DIR", str(tmp_path / ".hermes"))
    # Telemetry config also lives under $HOME.
    monkeypatch.setenv("CHAT4000_TELEMETRY_DISABLED", "1")
    # Clear cached device identity so each test gets a fresh UUID.
    import chat4000_hermes_plugin.key_store as ks

    ks._cached_instance = None
    yield
