"""chat4000 group ↔ Hermes session bindings."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chat4000_hermes_plugin.key_store import resolve_hermes_state_dir
from chat4000_hermes_plugin.session_binding import (
    HermesSessionCandidate,
    clear_chat4000_session_binding,
    get_chat4000_session_binding,
    list_hermes_session_candidates,
    pick_default_hermes_session,
    set_chat4000_session_binding,
)


class TestBindingPersistence:
    def test_get_returns_none_when_unset(self):
        assert get_chat4000_session_binding(account_id="default", group_id="g1") is None

    def test_set_and_get_roundtrip(self):
        target = {
            "session_key": "agent:main:telegram:dm:42",
            "agent_id": "main",
            "store_path": "/tmp/store.json",
            "session_id": "sess-xyz",
            "label": "Friday afternoon",
            "updated_at": 1700000000000,
            "last_preview": "Hello",
            "last_channel": "telegram",
        }
        set_chat4000_session_binding(
            account_id="default", group_id="g1", target=target
        )
        binding = get_chat4000_session_binding(account_id="default", group_id="g1")
        assert binding is not None
        assert binding.target_session_key == "agent:main:telegram:dm:42"
        assert binding.agent_id == "main"
        assert binding.label == "Friday afternoon"
        assert binding.updated_at == 1700000000000

    def test_set_overwrites(self):
        set_chat4000_session_binding(
            account_id="default", group_id="g1",
            target={"session_key": "first", "agent_id": "a", "session_id": "s1", "label": "x", "updated_at": 1},
        )
        set_chat4000_session_binding(
            account_id="default", group_id="g1",
            target={"session_key": "second", "agent_id": "a", "session_id": "s2", "label": "y", "updated_at": 2},
        )
        binding = get_chat4000_session_binding(account_id="default", group_id="g1")
        assert binding.target_session_key == "second"

    def test_clear_returns_true_if_existed(self):
        set_chat4000_session_binding(
            account_id="default", group_id="g1",
            target={"session_key": "x", "agent_id": "a", "session_id": "s", "label": "x", "updated_at": 1},
        )
        assert clear_chat4000_session_binding(account_id="default", group_id="g1") is True
        assert get_chat4000_session_binding(account_id="default", group_id="g1") is None

    def test_clear_returns_false_if_not_set(self):
        assert clear_chat4000_session_binding(account_id="default", group_id="g1") is False

    def test_isolated_per_group(self):
        set_chat4000_session_binding(
            account_id="default", group_id="g1",
            target={"session_key": "a", "agent_id": "x", "session_id": "1", "label": "x", "updated_at": 1},
        )
        set_chat4000_session_binding(
            account_id="default", group_id="g2",
            target={"session_key": "b", "agent_id": "x", "session_id": "2", "label": "x", "updated_at": 1},
        )
        assert get_chat4000_session_binding(account_id="default", group_id="g1").target_session_key == "a"
        assert get_chat4000_session_binding(account_id="default", group_id="g2").target_session_key == "b"


class TestHermesSessionEnumeration:
    def _write_sessions_json(self, agent_id: str, sessions: dict) -> Path:
        state_dir = resolve_hermes_state_dir()
        path = state_dir / "agents" / agent_id / "sessions" / "sessions.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sessions))
        return path

    def test_empty_when_no_agents_dir(self):
        # Fresh tmp_path → no agents dir.
        assert list_hermes_session_candidates() == []

    def test_lists_user_facing_sessions(self):
        self._write_sessions_json("main", {
            "agent:main:telegram:dm:42": {
                "sessionId": "s1",
                "updatedAt": 1700000000000,
                "displayName": "Friday convo",
            },
        })
        candidates = list_hermes_session_candidates()
        assert len(candidates) == 1
        assert candidates[0].label == "Friday convo"
        assert candidates[0].session_id == "s1"

    def test_filters_cron_acp_subagent_sessions(self):
        self._write_sessions_json("main", {
            "agent:main:telegram:dm:42": {
                "sessionId": "s1", "updatedAt": 1700000000000,
            },
            "agent:main:cron:nightly:reports": {
                "sessionId": "cron1", "updatedAt": 1700000001000,
            },
            "agent:main:acp:foo": {
                "sessionId": "acp1", "updatedAt": 1700000002000,
            },
            "agent:main:subagent:parallel": {
                "sessionId": "sub1", "updatedAt": 1700000003000,
            },
        })
        candidates = list_hermes_session_candidates()
        # Only the user-facing dm session survives the filter.
        assert len(candidates) == 1
        assert candidates[0].session_id == "s1"

    def test_sorted_by_updated_at_desc(self):
        self._write_sessions_json("main", {
            "agent:main:telegram:dm:1": {"sessionId": "older", "updatedAt": 1000},
            "agent:main:telegram:dm:2": {"sessionId": "newer", "updatedAt": 2000},
        })
        candidates = list_hermes_session_candidates()
        assert [c.session_id for c in candidates] == ["newer", "older"]

    def test_pick_default_returns_latest(self):
        self._write_sessions_json("main", {
            "agent:main:telegram:dm:1": {"sessionId": "older", "updatedAt": 1000},
            "agent:main:telegram:dm:2": {"sessionId": "newer", "updatedAt": 2000},
        })
        d = pick_default_hermes_session()
        assert d is not None
        assert d.session_id == "newer"

    def test_pick_default_none_when_no_sessions(self):
        assert pick_default_hermes_session() is None

    def test_malformed_sessions_file_ignored(self):
        state_dir = resolve_hermes_state_dir()
        path = state_dir / "agents" / "main" / "sessions" / "sessions.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json{")
        # Should NOT raise; just returns empty.
        assert list_hermes_session_candidates() == []
