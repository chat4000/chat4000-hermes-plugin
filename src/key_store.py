"""Plugin-managed group-key storage at ~/.hermes/plugins/chat4000/keys/.

Port of clawconnect-plugin/src/key-store.ts. The Hermes home directory
defaults to ~/.hermes (matches Hermes core's expectations); the
HERMES_STATE_DIR env var overrides for tests / multi-profile setups.

Key files are written with mode 0o600 — the group key IS the auth
credential, so only the user should be able to read it. We also chown
to the right uid when running as root and the parent dir is owned
elsewhere (matches the TS impl's drop-privileges behaviour).
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .crypto import derive_group_id, parse_group_key

_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._-]")


def _sanitize_account_id(account_id: str) -> str:
    value = (account_id or "").strip() or "default"
    return _SANITIZE_RE.sub("_", value)


def resolve_hermes_home() -> Path:
    """The HERMES_HOME env var wins; fall back to ~/.hermes.

    Matches Hermes core's `get_hermes_home()` from `hermes_cli/config.py`
    so plugin state lives alongside Hermes' own state."""
    env_home = os.environ.get("HERMES_HOME", "").strip()
    if env_home:
        return Path(env_home).expanduser()
    return Path.home() / ".hermes"


def resolve_hermes_state_dir() -> Path:
    """HERMES_STATE_DIR overrides; otherwise the hermes home dir is the
    state dir (Hermes doesn't currently split them, but the env var lets
    tests and multi-profile setups stash plugin state separately)."""
    env_state = os.environ.get("HERMES_STATE_DIR", "").strip()
    if env_state:
        return Path(env_state).expanduser()
    return resolve_hermes_home()


def resolve_chat4000_plugin_dir() -> Path:
    return resolve_hermes_state_dir() / "plugins" / "chat4000"


def resolve_chat4000_key_file_path(account_id: str) -> Path:
    return (
        resolve_chat4000_plugin_dir()
        / "keys"
        / f"{_sanitize_account_id(account_id)}.json"
    )


def resolve_chat4000_instance_file_path() -> Path:
    return resolve_chat4000_plugin_dir() / "instance.json"


# ─── Stored group key (the durable identity) ──────────────────────────────


@dataclass
class StoredChat4000Key:
    group_key_bytes: bytes
    group_id: str
    path: Path


def load_stored_group_key(account_id: str) -> Optional[StoredChat4000Key]:
    """Read the per-account key file. Returns None on missing / malformed /
    permission errors. Never raises — callers branch on configured state."""
    file_path = resolve_chat4000_key_file_path(account_id)
    if not file_path.exists():
        return None
    try:
        raw = file_path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
        if parsed.get("version") != 1 or not isinstance(parsed.get("groupKey"), str):
            return None
        group_key_bytes = parse_group_key(parsed["groupKey"])
        group_id = parsed.get("groupId") or derive_group_id(group_key_bytes)
        return StoredChat4000Key(
            group_key_bytes=group_key_bytes,
            group_id=group_id,
            path=file_path,
        )
    except Exception:
        return None


def save_stored_group_key(account_id: str, group_key_bytes: bytes) -> StoredChat4000Key:
    """Atomic write at mode 0o600. Creates parent dirs as needed.

    Drops ownership to the preferred owner when we're running as root and
    the parent dir is owned elsewhere — necessary when Hermes' gateway
    container runs as root but the user's home is owned by uid 1000."""
    file_path = resolve_chat4000_key_file_path(account_id)
    file_path.parent.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc).isoformat()
    existing = load_stored_group_key(account_id)
    payload = {
        "version": 1,
        "accountId": _sanitize_account_id(account_id),
        "groupKey": _b64url_no_pad(group_key_bytes),
        "groupId": derive_group_id(group_key_bytes),
        "createdAt": now if existing is None else existing_created_at(file_path, now),
        "updatedAt": now,
    }
    file_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.chmod(file_path, 0o600)
    _apply_owner_if_needed([file_path.parent, file_path])
    return StoredChat4000Key(
        group_key_bytes=group_key_bytes,
        group_id=payload["groupId"],
        path=file_path,
    )


def existing_created_at(file_path: Path, fallback: str) -> str:
    try:
        raw = file_path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
        return parsed.get("createdAt") or fallback
    except Exception:
        return fallback


def _b64url_no_pad(b: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


# ─── Per-install device identity (used in SenderInfo.device_id) ────────────


@dataclass
class Chat4000InstanceIdentity:
    device_id: str       # stable UUID across plugin restarts
    device_name: str
    path: Path


_cached_instance: Optional[Chat4000InstanceIdentity] = None


def resolve_chat4000_instance_identity() -> Chat4000InstanceIdentity:
    """Lazy-load or mint a per-install identity. Persisted at
    ~/.hermes/plugins/chat4000/instance.json. Falls back to a process-local
    identity if disk is unavailable (read-only fs, sandboxing, etc.)."""
    global _cached_instance
    if _cached_instance is not None:
        return _cached_instance

    file_path = resolve_chat4000_instance_file_path()
    default_name = os.uname().nodename if hasattr(os, "uname") else "Hermes Plugin"

    if file_path.exists():
        try:
            parsed = json.loads(file_path.read_text(encoding="utf-8"))
            if parsed.get("version") == 1 and isinstance(parsed.get("deviceId"), str):
                _cached_instance = Chat4000InstanceIdentity(
                    device_id=parsed["deviceId"],
                    device_name=parsed.get("deviceName") or default_name,
                    path=file_path,
                )
                return _cached_instance
        except Exception:
            pass  # rewrite

    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "version": 1,
        "deviceId": str(uuid.uuid4()),
        "deviceName": default_name,
        "createdAt": now,
        "updatedAt": now,
    }
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.chmod(file_path, 0o600)
        _apply_owner_if_needed([file_path.parent, file_path])
    except Exception:
        # Read-only fs — fall through to process-local identity.
        pass

    _cached_instance = Chat4000InstanceIdentity(
        device_id=payload["deviceId"],
        device_name=payload["deviceName"],
        path=file_path,
    )
    return _cached_instance


# ─── State access inspection (for CLI permission diagnostics) ──────────────


@dataclass
class Chat4000StateAccess:
    state_dir: Path
    plugin_dir: Path
    keys_dir: Path
    key_file_path: Path
    current_uid: Optional[int]
    current_gid: Optional[int]
    preferred_owner_uid: Optional[int]
    preferred_owner_gid: Optional[int]
    can_auto_repair_ownership: bool
    has_ownership_mismatch: bool


def inspect_chat4000_state_access(account_id: str) -> Chat4000StateAccess:
    state_dir = resolve_hermes_state_dir()
    plugin_dir = resolve_chat4000_plugin_dir()
    keys_dir = plugin_dir / "keys"
    key_file_path = keys_dir / f"{_sanitize_account_id(account_id)}.json"
    current_uid = os.getuid() if hasattr(os, "getuid") else None
    current_gid = os.getgid() if hasattr(os, "getgid") else None
    owner_uid, owner_gid = _resolve_preferred_owner(key_file_path)
    has_mismatch = (
        current_uid is not None
        and owner_uid is not None
        and current_uid != owner_uid
    )
    can_repair = current_uid == 0 and owner_uid is not None
    return Chat4000StateAccess(
        state_dir=state_dir,
        plugin_dir=plugin_dir,
        keys_dir=keys_dir,
        key_file_path=key_file_path,
        current_uid=current_uid,
        current_gid=current_gid,
        preferred_owner_uid=owner_uid,
        preferred_owner_gid=owner_gid,
        can_auto_repair_ownership=can_repair,
        has_ownership_mismatch=has_mismatch,
    )


def _resolve_preferred_owner(target: Path) -> tuple[Optional[int], Optional[int]]:
    current = target.parent.resolve()
    while True:
        if current.exists():
            try:
                st = current.stat()
                return (st.st_uid, st.st_gid)
            except OSError:
                return (None, None)
        parent = current.parent
        if parent == current:
            return (None, None)
        current = parent


def _apply_owner_if_needed(paths: list[Path]) -> None:
    """When running as root and the parent dir has a non-root owner, chown
    the freshly-written files so the operator can still read them after
    container/daemon teardown. POSIX-only; no-op on Windows."""
    if sys.platform == "win32":
        return
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    if not paths:
        return
    owner_uid, owner_gid = _resolve_preferred_owner(paths[0])
    if owner_uid is None or owner_gid is None:
        return
    for p in paths:
        try:
            if p.exists():
                os.chown(p, owner_uid, owner_gid)
        except OSError:
            pass  # best-effort
