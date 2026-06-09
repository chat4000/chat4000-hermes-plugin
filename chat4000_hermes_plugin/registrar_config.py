"""Shared registrar configuration for CLI, onboarding, and Matrix commands."""

from __future__ import annotations

import os
from pathlib import Path

from .key_store import resolve_chat4000_plugin_dir
from .matrix.registrar_client import RegistrarClient

# Per-environment registrar. The registrar you pair against decides everything:
# the stage registrar mints stage creds whose gateway_url points at the stage
# gateway, so the running plugin follows automatically (it just uses the stored
# creds). Select with `chat4000 pair --stage` or CHAT4000_ENV=stage.
REGISTRAR_URLS = {
    "production": "https://registrar.chat4000.com",
    "stage": "https://registrar.stgcht4.duckdns.org",
}

# Static shared service token. It gates pairing-code registration, status polling,
# and plugin-version lookup (never content) — basic-auth-grade by design: it ships
# in the client, so treat it as public. Override with CHAT4000_SERVICE_TOKEN.
DEFAULT_SERVICE_TOKEN = "chat4000_svc_72ee3b80a16f826a173c65450cadd107d5f6912d4d96135a"  # noqa: S105


def env_file_path() -> Path:
    """Where the chosen environment is persisted so it survives a fresh shell."""
    return resolve_chat4000_plugin_dir() / "env"


def load_persisted_env() -> str:
    try:
        value = env_file_path().read_text(encoding="utf-8").strip().lower()
        return value if value in REGISTRAR_URLS else ""
    except OSError:
        return ""


def persist_env(env: str) -> None:
    """Record the environment selection durably (best-effort, never raises)."""
    if env not in REGISTRAR_URLS:
        return
    try:
        path = env_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(env + "\n", encoding="utf-8")
    except OSError:
        pass


def resolve_env() -> str:
    """Resolve the active chat4000 environment."""
    env = os.environ.get("CHAT4000_ENV", "").strip().lower()
    if env in REGISTRAR_URLS:
        return env
    return load_persisted_env() or "production"


def resolve_registrar_url() -> str:
    """Resolve the registrar base URL for the active environment."""
    explicit = os.environ.get("CHAT4000_REGISTRAR_URL", "").strip()
    return explicit or REGISTRAR_URLS[resolve_env()]


def build_registrar_client() -> RegistrarClient:
    """Build a registrar client with the configured shared service token."""
    token = os.environ.get("CHAT4000_SERVICE_TOKEN", "").strip() or DEFAULT_SERVICE_TOKEN
    return RegistrarClient(resolve_registrar_url(), token)
