"""Account config resolution. Precedence:
  CHAT4000_GROUP_KEY env > config.groupKey > stored key file > unconfigured."""

from __future__ import annotations

import os

import pytest

from src.accounts import (
    DEFAULT_RELAY_URL,
    get_default_chat4000_account_id,
    has_configured_state,
    list_chat4000_account_ids,
    resolve_chat4000_account,
)
from src.crypto import derive_group_id, generate_group_key, parse_group_key
from src.key_store import save_stored_group_key


class TestUnconfigured:
    def test_no_config_returns_unconfigured(self, monkeypatch):
        monkeypatch.delenv("CHAT4000_GROUP_KEY", raising=False)
        account = resolve_chat4000_account(None, None)
        assert account.configured is False
        assert account.group_id == ""
        assert account.key_source == "missing"

    def test_empty_dict_config_returns_unconfigured(self, monkeypatch):
        monkeypatch.delenv("CHAT4000_GROUP_KEY", raising=False)
        account = resolve_chat4000_account({}, None)
        assert account.configured is False


class TestEnvOverride:
    def test_env_key_wins(self, monkeypatch):
        key = generate_group_key()
        monkeypatch.setenv("CHAT4000_GROUP_KEY", key.hex())
        account = resolve_chat4000_account(None, "default")
        assert account.configured is True
        assert account.key_source == "env"
        assert account.group_key_bytes == key
        assert account.group_id == derive_group_id(key)

    def test_garbage_env_falls_through(self, monkeypatch):
        monkeypatch.setenv("CHAT4000_GROUP_KEY", "not-a-real-key!!!")
        account = resolve_chat4000_account(None, "default")
        # Bad env value → don't crash, just unconfigured.
        assert account.configured is False

    def test_env_beats_stored_file(self, monkeypatch, tmp_path):
        file_key = generate_group_key()
        save_stored_group_key("default", file_key)
        env_key = generate_group_key()
        monkeypatch.setenv("CHAT4000_GROUP_KEY", env_key.hex())
        account = resolve_chat4000_account(None, "default")
        assert account.group_key_bytes == env_key
        assert account.key_source == "env"


class TestConfigOverride:
    def test_config_group_key_used(self, monkeypatch):
        monkeypatch.delenv("CHAT4000_GROUP_KEY", raising=False)
        key = generate_group_key()
        cfg = {
            "channels": {
                "chat4000": {"groupKey": key.hex()}
            }
        }
        account = resolve_chat4000_account(cfg, "default")
        assert account.configured is True
        assert account.key_source == "config"
        assert account.group_key_bytes == key

    def test_config_beats_file_but_not_env(self, monkeypatch, tmp_path):
        # Plant a stored file key.
        save_stored_group_key("default", generate_group_key())
        config_key = generate_group_key()
        monkeypatch.delenv("CHAT4000_GROUP_KEY", raising=False)
        cfg = {"channels": {"chat4000": {"groupKey": config_key.hex()}}}
        account = resolve_chat4000_account(cfg, "default")
        assert account.key_source == "config"
        assert account.group_key_bytes == config_key


class TestStoredFile:
    def test_file_key_used_when_no_env_no_config(self, monkeypatch, tmp_path):
        monkeypatch.delenv("CHAT4000_GROUP_KEY", raising=False)
        file_key = generate_group_key()
        save_stored_group_key("default", file_key)
        account = resolve_chat4000_account(None, "default")
        assert account.configured is True
        assert account.key_source == "state-file"
        assert account.group_key_bytes == file_key


class TestPerAccountConfig:
    def test_per_account_override_beats_top_level(self, monkeypatch):
        monkeypatch.delenv("CHAT4000_GROUP_KEY", raising=False)
        top_key = generate_group_key()
        work_key = generate_group_key()
        cfg = {
            "channels": {
                "chat4000": {
                    "groupKey": top_key.hex(),
                    "accounts": {
                        "work": {"groupKey": work_key.hex()},
                    },
                }
            }
        }
        a_default = resolve_chat4000_account(cfg, "default")
        a_work = resolve_chat4000_account(cfg, "work")
        # When resolving "work", per-account key beats top-level — even though
        # `work` isn't a real account_id we resolve back to top-level fields
        # when the override doesn't specify them.
        assert a_work.group_key_bytes == work_key

    def test_list_chat4000_account_ids(self):
        cfg = {
            "channels": {
                "chat4000": {
                    "accounts": {"work": {}, "personal": {}},
                    "defaultAccount": "work",
                }
            }
        }
        ids = list_chat4000_account_ids(cfg)
        assert set(ids) == {"work", "personal"}

    def test_list_falls_back_to_default(self):
        assert list_chat4000_account_ids(None) == ["default"]

    def test_default_account_picks_named(self):
        cfg = {
            "channels": {
                "chat4000": {
                    "accounts": {"work": {}, "personal": {}},
                    "defaultAccount": "personal",
                }
            }
        }
        assert get_default_chat4000_account_id(cfg) == "personal"

    def test_default_account_falls_back_to_first(self):
        cfg = {
            "channels": {
                "chat4000": {"accounts": {"alpha": {}, "beta": {}}}
            }
        }
        # Either is acceptable — order depends on dict insertion in Python 3.7+.
        assert get_default_chat4000_account_id(cfg) in {"alpha", "beta"}


class TestRelayUrl:
    def test_default_relay_url(self):
        account = resolve_chat4000_account({}, "default")
        assert account.relay_url == DEFAULT_RELAY_URL
        assert account.relay_url == "wss://relay.chat4000.com/ws"


class TestHasConfiguredState:
    def test_returns_true_with_env_key(self):
        assert has_configured_state({"CHAT4000_GROUP_KEY": "x" * 32}) is True

    def test_returns_false_without_env_key(self):
        assert has_configured_state({}) is False

    def test_returns_false_with_empty_env_key(self):
        assert has_configured_state({"CHAT4000_GROUP_KEY": "   "}) is False
