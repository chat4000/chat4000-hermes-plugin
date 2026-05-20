"""Account resolution: merges top-level config + per-account overrides +
env vars, loads the plugin-managed key file, and returns a single
ResolvedChat4000Account that the adapter can use.

Port of clawconnect-plugin/src/accounts.ts. The resolution order is:

  1. CHAT4000_GROUP_KEY env var          (escape hatch / migration)
  2. config.groupKey from Hermes YAML    (legacy / manual override)
  3. plugin-managed key file on disk     (PRIMARY workflow — `hermes chat4000 pair`)

The relay URL is hardcoded to `wss://relay.chat4000.com/ws` — not
operator-configurable because the relay is a shared zero-knowledge
service. If a self-hosted relay is needed later, expose it as a
HERMES_CHAT4000_RELAY_URL env var; protocol stays unchanged.
"""

from __future__ import annotations

import os
from typing import Optional

from .crypto import derive_group_id, parse_group_key
from .key_store import (
    load_stored_group_key,
    resolve_chat4000_key_file_path,
)
from .protocol_types import Chat4000AccountConfig, Chat4000Config, ResolvedChat4000Account

DEFAULT_RELAY_URL = "wss://relay.chat4000.com/ws"


def _get_channel_config(cfg: dict | None) -> Chat4000Config:
    """Pull the chat4000 channel block out of a Hermes config dict.
    Defensive against partial configs — every field has a default."""
    cfg = cfg or {}
    raw = ((cfg.get("channels") or {}).get("chat4000")) or {}
    accounts_raw = raw.get("accounts") or {}
    accounts = {
        aid: Chat4000AccountConfig(
            enabled=acc.get("enabled", True),
            pairing_log_level=acc.get("pairingLogLevel", acc.get("pairing_log_level", "info")),
            runtime_log_level=acc.get("runtimeLogLevel", acc.get("runtime_log_level", "info")),
            release_channel=acc.get("releaseChannel", acc.get("release_channel", "production")),
            group_key=acc.get("groupKey", acc.get("group_key")),
            text_chunk_limit=acc.get("textChunkLimit", acc.get("text_chunk_limit", 4096)),
            block_streaming=acc.get("blockStreaming", acc.get("block_streaming", False)),
        )
        for aid, acc in accounts_raw.items()
    }
    return Chat4000Config(
        enabled=raw.get("enabled", True),
        pairing_log_level=raw.get("pairingLogLevel", raw.get("pairing_log_level", "info")),
        runtime_log_level=raw.get("runtimeLogLevel", raw.get("runtime_log_level", "info")),
        release_channel=raw.get("releaseChannel", raw.get("release_channel", "production")),
        group_key=raw.get("groupKey", raw.get("group_key")),
        text_chunk_limit=raw.get("textChunkLimit", raw.get("text_chunk_limit", 4096)),
        block_streaming=raw.get("blockStreaming", raw.get("block_streaming", False)),
        accounts=accounts,
        default_account=raw.get("defaultAccount", raw.get("default_account")),
    )


def list_chat4000_account_ids(cfg: dict | None) -> list[str]:
    channel = _get_channel_config(cfg)
    ids = list(channel.accounts.keys())
    return ids if ids else [channel.default_account or "default"]


def get_default_chat4000_account_id(cfg: dict | None) -> str:
    channel = _get_channel_config(cfg)
    ids = list(channel.accounts.keys())
    if channel.default_account and channel.default_account in ids:
        return channel.default_account
    return ids[0] if ids else (channel.default_account or "default")


def resolve_chat4000_account(
    cfg: dict | None = None, account_id: Optional[str] = None
) -> ResolvedChat4000Account:
    """The single entry point the adapter uses. Returns a fully-resolved
    account; `configured=False` means the caller should prompt the user
    to run `hermes chat4000 pair` before connecting."""
    channel = _get_channel_config(cfg)
    account_id = account_id or get_default_chat4000_account_id(cfg)
    override = channel.accounts.get(account_id, Chat4000AccountConfig())

    # Merge: per-account override beats top-level channel config.
    merged = Chat4000AccountConfig(
        enabled=(override.enabled if override.enabled is not None else channel.enabled),
        pairing_log_level=override.pairing_log_level or channel.pairing_log_level,
        runtime_log_level=override.runtime_log_level or channel.runtime_log_level,
        release_channel=override.release_channel or channel.release_channel,
        group_key=override.group_key or channel.group_key,
        text_chunk_limit=override.text_chunk_limit or channel.text_chunk_limit,
        block_streaming=override.block_streaming or channel.block_streaming,
    )

    group_key_bytes = b""
    group_id = ""
    key_source: str = "missing"
    key_file_path = str(resolve_chat4000_key_file_path(account_id))

    env_raw = (os.environ.get("CHAT4000_GROUP_KEY") or "").strip()
    config_raw = (merged.group_key or "").strip()

    if env_raw:
        try:
            group_key_bytes = parse_group_key(env_raw)
            group_id = derive_group_id(group_key_bytes)
            key_source = "env"
        except Exception:
            group_key_bytes = b""
    elif config_raw:
        try:
            group_key_bytes = parse_group_key(config_raw)
            group_id = derive_group_id(group_key_bytes)
            key_source = "config"
        except Exception:
            group_key_bytes = b""
    else:
        stored = load_stored_group_key(account_id)
        if stored is not None:
            group_key_bytes = stored.group_key_bytes
            group_id = stored.group_id
            key_source = "state-file"

    configured = len(group_key_bytes) == 32

    return ResolvedChat4000Account(
        account_id=account_id,
        enabled=merged.enabled,
        configured=configured,
        relay_url=DEFAULT_RELAY_URL,
        pairing_log_level=merged.pairing_log_level,
        runtime_log_level=merged.runtime_log_level,
        group_id=group_id,
        group_key_bytes=group_key_bytes,
        key_file_path=key_file_path,
        key_source=key_source,  # type: ignore[arg-type]
        config=merged,
    )


def has_configured_state(env: dict[str, str] | None = None) -> bool:
    """Used by Hermes' setup wizard to detect env-only configuration —
    so the wizard can skip prompts when CHAT4000_GROUP_KEY is set."""
    env = env or {}
    raw = (env.get("CHAT4000_GROUP_KEY") or "").strip()
    return bool(raw)
