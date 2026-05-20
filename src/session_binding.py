"""Bind a chat4000 group to a single Hermes agent session.

Port of clawconnect-plugin/src/session-binding.ts, adapted to Hermes'
filesystem layout. We're a single-session plugin (per the spec the user
agreed: "1 session for now, no sessions list"), so this module's surface
is intentionally smaller than the TS plugin's. The full enumeration /
multi-session UI is deferred until Tier 2-D.

Hermes session-store discovery:
  - Hermes writes session metadata to ~/.hermes/agents/<agent_id>/sessions/sessions.json
  - We pick the latest-touched session as the default binding
  - The binding is persisted at ~/.hermes/plugins/chat4000/session-bindings.json
    so a fresh plugin start reconnects to the same Hermes session
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .key_store import resolve_chat4000_plugin_dir, resolve_hermes_state_dir


# ─── Stored binding ──────────────────────────────────────────────────────


@dataclass
class Chat4000SessionBinding:
    account_id: str
    group_id: str
    target_session_key: str
    agent_id: str
    store_path: str
    session_id: str
    label: str
    updated_at: int
    bound_at: str
    last_preview: Optional[str] = None
    last_channel: Optional[str] = None


def _resolve_bindings_file_path() -> Path:
    return resolve_chat4000_plugin_dir() / "session-bindings.json"


def _binding_key(account_id: str, group_id: str) -> str:
    return f"{(account_id or 'default').strip()}:{group_id.strip()}"


def _load_bindings() -> dict[str, dict]:
    file_path = _resolve_bindings_file_path()
    if not file_path.exists():
        return {}
    try:
        parsed = json.loads(file_path.read_text(encoding="utf-8"))
        if parsed.get("version") != 1:
            return {}
        return parsed.get("bindings") or {}
    except Exception:
        return {}


def _save_bindings(bindings: dict[str, dict]) -> None:
    file_path = _resolve_bindings_file_path()
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(
        json.dumps({"version": 1, "bindings": bindings}, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        os.chmod(file_path, 0o600)
    except OSError:
        pass


def get_chat4000_session_binding(
    *, account_id: str, group_id: str
) -> Optional[Chat4000SessionBinding]:
    bindings = _load_bindings()
    raw = bindings.get(_binding_key(account_id, group_id))
    if raw is None:
        return None
    return Chat4000SessionBinding(
        account_id=raw.get("accountId", account_id),
        group_id=raw.get("groupId", group_id),
        target_session_key=raw.get("targetSessionKey", ""),
        agent_id=raw.get("agentId", ""),
        store_path=raw.get("storePath", ""),
        session_id=raw.get("sessionId", ""),
        label=raw.get("label", ""),
        updated_at=int(raw.get("updatedAt") or 0),
        bound_at=raw.get("boundAt", ""),
        last_preview=raw.get("lastPreview"),
        last_channel=raw.get("lastChannel"),
    )


def set_chat4000_session_binding(
    *, account_id: str, group_id: str, target: dict[str, Any]
) -> Chat4000SessionBinding:
    bindings = _load_bindings()
    binding = Chat4000SessionBinding(
        account_id=account_id,
        group_id=group_id,
        target_session_key=target.get("session_key", target.get("sessionKey", "")),
        agent_id=target.get("agent_id", target.get("agentId", "")),
        store_path=target.get("store_path", target.get("storePath", "")),
        session_id=target.get("session_id", target.get("sessionId", "")),
        label=target.get("label", ""),
        updated_at=int(target.get("updated_at") or target.get("updatedAt") or 0),
        bound_at=datetime.now(timezone.utc).isoformat(),
        last_preview=target.get("last_preview") or target.get("lastPreview"),
        last_channel=target.get("last_channel") or target.get("lastChannel"),
    )
    bindings[_binding_key(account_id, group_id)] = {
        "accountId": binding.account_id,
        "groupId": binding.group_id,
        "targetSessionKey": binding.target_session_key,
        "agentId": binding.agent_id,
        "storePath": binding.store_path,
        "sessionId": binding.session_id,
        "label": binding.label,
        "updatedAt": binding.updated_at,
        "boundAt": binding.bound_at,
        "lastPreview": binding.last_preview,
        "lastChannel": binding.last_channel,
    }
    _save_bindings(bindings)
    return binding


def clear_chat4000_session_binding(*, account_id: str, group_id: str) -> bool:
    bindings = _load_bindings()
    key = _binding_key(account_id, group_id)
    if key not in bindings:
        return False
    del bindings[key]
    _save_bindings(bindings)
    return True


# ─── Hermes session enumeration (used to auto-pick the latest) ───────────


@dataclass
class HermesSessionCandidate:
    session_key: str
    agent_id: str
    session_id: str
    store_path: str
    updated_at: int
    label: str
    last_preview: Optional[str] = None
    last_channel: Optional[str] = None
    last_to: Optional[str] = None
    last_account_id: Optional[str] = None


def _list_known_agent_ids() -> list[str]:
    agents_dir = resolve_hermes_state_dir() / "agents"
    if not agents_dir.exists():
        return ["main"]
    names = sorted(
        entry.name.strip()
        for entry in agents_dir.iterdir()
        if entry.is_dir() and entry.name.strip()
    )
    return names if names else ["main"]


def _resolve_session_store_paths() -> list[Path]:
    paths: set[Path] = set()
    state_dir = resolve_hermes_state_dir()
    for agent_id in _list_known_agent_ids():
        paths.add(state_dir / "agents" / agent_id / "sessions" / "sessions.json")
    return sorted(paths)


def _is_user_facing_session_key(session_key: str) -> bool:
    if not session_key.startswith("agent:"):
        return False
    return (
        ":cron:" not in session_key
        and ":acp:" not in session_key
        and ":subagent:" not in session_key
    )


def list_hermes_session_candidates() -> list[HermesSessionCandidate]:
    candidates: list[HermesSessionCandidate] = []
    for store_path in _resolve_session_store_paths():
        if not store_path.exists():
            continue
        try:
            parsed = json.loads(store_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(parsed, dict):
            continue
        for session_key, entry in parsed.items():
            if not _is_user_facing_session_key(session_key):
                continue
            if not isinstance(entry, dict):
                continue
            session_id = entry.get("sessionId") or entry.get("session_id")
            updated_at = entry.get("updatedAt") or entry.get("updated_at")
            if not session_id or not isinstance(updated_at, (int, float)):
                continue
            agent_id = session_key.split(":")[1] if session_key.startswith("agent:") else "main"
            label = (
                entry.get("displayName")
                or entry.get("label")
                or entry.get("subject")
                or session_key
            )
            candidates.append(
                HermesSessionCandidate(
                    session_key=session_key,
                    agent_id=agent_id,
                    session_id=str(session_id),
                    store_path=str(store_path),
                    updated_at=int(updated_at),
                    label=str(label),
                    last_preview=entry.get("lastPreview") or entry.get("last_preview"),
                    last_channel=entry.get("lastChannel") or entry.get("last_channel"),
                    last_to=entry.get("lastTo") or entry.get("last_to"),
                    last_account_id=entry.get("lastAccountId") or entry.get("last_account_id"),
                )
            )
    candidates.sort(key=lambda c: c.updated_at, reverse=True)
    return candidates


def pick_default_hermes_session() -> Optional[HermesSessionCandidate]:
    """The single-session model — most recent user-facing session wins.
    Returns None when Hermes has never been used (cold install)."""
    candidates = list_hermes_session_candidates()
    return candidates[0] if candidates else None
