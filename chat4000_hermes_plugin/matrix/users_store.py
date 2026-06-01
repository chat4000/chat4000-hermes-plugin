"""Known-users store — the set of paired user MXIDs the plugin serves.

When `chat4000 pair` completes, the paired user's MXID is recorded here. The
running gateway adapter loads this on connect and (a) invites each to the space +
control room and (b) shares room keys with them (`set_members`). Idempotent —
inviting an already-joined user is benign, and key-sharing a known session is a
no-op.

Decoupling pairing (CLI process) from inviting (gateway process) this way means
the CLI never needs the gateway socket or the crypto store.

Stored at ~/.hermes/plugins/chat4000/known-users-<account>.json (mode 0600).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ..key_store import resolve_chat4000_plugin_dir


def _path(account_id: str = "default") -> Path:
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in (account_id or "default"))
    return resolve_chat4000_plugin_dir() / f"known-users-{safe}.json"


def load_known_users(account_id: str = "default") -> list[str]:
    p = _path(account_id)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return [str(u) for u in data.get("users", [])]
    except Exception:
        return []


def add_known_user(user_id: str, account_id: str = "default") -> list[str]:
    users = load_known_users(account_id)
    if user_id not in users:
        users.append(user_id)
        _save(users, account_id)
    return users


def _save(users: list[str], account_id: str) -> None:
    p = _path(account_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"users": users}, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
