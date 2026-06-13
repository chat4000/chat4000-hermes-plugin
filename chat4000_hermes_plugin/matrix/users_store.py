"""Known-users store — the plugin's user MXID(s), durably recorded.

One plugin = exactly ONE human user (protocol B): the account `PUT /user` (C.2)
creates at setup, recorded here by `setup_flow.ensure_setup` (and, redundantly
but idempotently, when a pairing completes — every code is bound to that same
user, so re-adding is a no-op). Pairing never creates users; it only adds
devices to the one user already in this store.

The store keeps its list shape for backwards compatibility: a legacy store
written before the one-user redesign may contain several MXIDs, and the gateway
keeps serving all of them (dropping entries would orphan their rooms). New
writes only ever add the plugin's single ensured user.

The running gateway adapter loads this on connect and (a) invites each user to
the space + control room and (b) shares room keys with them (`set_members`).
Idempotent — inviting an already-joined user is benign, and key-sharing a known
session is a no-op.

Decoupling pairing (CLI process) from inviting (gateway process) this way means
the CLI never needs the gateway socket or the crypto store.

Stored at ~/.hermes/plugins/chat4000/known-users-<account>.json (mode 0600).

A sibling `onboarded-<account>.json` durably records which users have already
received their auto-created INITIAL session room (mapping user_id → room_id). The
gateway re-reads known-users and re-invites on every restart; this store is what
stops it from minting a SECOND initial room for an already-onboarded user (the
per-connection `_invited` set is not durable). Kept separate from known-users so
the known-users schema other code reads stays untouched.
"""

from __future__ import annotations

import contextlib
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
    except (OSError, json.JSONDecodeError, AttributeError, TypeError):
        # Missing / unreadable / malformed store → no known users (callers branch).
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
    with contextlib.suppress(OSError):
        os.chmod(p, 0o600)


# ─── onboarded store (durable "already got an initial session room") ──────────


def _onboarded_path(account_id: str = "default") -> Path:
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in (account_id or "default"))
    return resolve_chat4000_plugin_dir() / f"onboarded-{safe}.json"


def load_onboarded(account_id: str = "default") -> dict[str, str]:
    """Map of user_id → their auto-created initial session room_id. Empty when the
    store is missing/unreadable (callers then create + record the room)."""
    p = _onboarded_path(account_id)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        users = data.get("users", {})
        if not isinstance(users, dict):
            return {}
        return {str(k): str(v) for k, v in users.items()}
    except (OSError, json.JSONDecodeError, AttributeError, TypeError):
        return {}


def mark_onboarded(user_id: str, room_id: str, account_id: str = "default") -> dict[str, str]:
    """Durably record that `user_id` has received their initial session room. This
    is the dedupe that stops a restart (which re-reads known-users and re-invites)
    from minting a SECOND initial room. Idempotent."""
    onboarded = load_onboarded(account_id)
    if onboarded.get(user_id) != room_id:
        onboarded[user_id] = room_id
        _save_onboarded(onboarded, account_id)
    return onboarded


def _save_onboarded(onboarded: dict[str, str], account_id: str) -> None:
    p = _onboarded_path(account_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"users": onboarded}, indent=2) + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(p, 0o600)
