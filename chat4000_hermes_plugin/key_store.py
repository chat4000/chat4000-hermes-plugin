"""Plugin state directory resolution (~/.hermes/plugins/chat4000/).

The Hermes home defaults to ~/.hermes (matches Hermes core's `get_hermes_home()`);
`HERMES_HOME` / `HERMES_STATE_DIR` override it (tests / multi-profile). v2 stores
the Matrix bot creds, the crypto store, the env selection, and logs under here —
see creds_store, crypto_driver, cli, error_log.
"""

from __future__ import annotations

import os
from pathlib import Path


def resolve_hermes_home() -> Path:
    """The HERMES_HOME env var wins; fall back to ~/.hermes."""
    env_home = os.environ.get("HERMES_HOME", "").strip()
    if env_home:
        return Path(env_home).expanduser()
    return Path.home() / ".hermes"


def resolve_hermes_state_dir() -> Path:
    """HERMES_STATE_DIR overrides; otherwise the hermes home dir is the state dir
    (lets tests and multi-profile setups stash plugin state separately)."""
    env_state = os.environ.get("HERMES_STATE_DIR", "").strip()
    if env_state:
        return Path(env_state).expanduser()
    return resolve_hermes_home()


def resolve_chat4000_plugin_dir() -> Path:
    return resolve_hermes_state_dir() / "plugins" / "chat4000"


def resolve_chat4000_ready_marker() -> Path:
    """Touched by the adapter once the gateway is fully connected + bootstrapped.
    The install wizard deletes it, restarts the gateway, and polls for it to
    reappear — the 'gateway is fully up' signal for the gateway-first flow."""
    return resolve_chat4000_plugin_dir() / "ready"
