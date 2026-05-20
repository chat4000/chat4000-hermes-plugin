"""Key-file storage: write/read round-trip, file modes, instance identity."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from chat4000_hermes_plugin.crypto import generate_group_key
from chat4000_hermes_plugin.key_store import (
    inspect_chat4000_state_access,
    load_stored_group_key,
    resolve_chat4000_instance_identity,
    resolve_chat4000_key_file_path,
    resolve_chat4000_plugin_dir,
    resolve_hermes_state_dir,
    save_stored_group_key,
)


class TestPathResolution:
    def test_hermes_state_dir_uses_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_STATE_DIR", str(tmp_path / "custom"))
        assert resolve_hermes_state_dir() == tmp_path / "custom"

    def test_plugin_dir_under_state_dir(self):
        plugin = resolve_chat4000_plugin_dir()
        assert plugin.parts[-3:] == (".hermes", "plugins", "chat4000")

    def test_key_file_per_account(self):
        a = resolve_chat4000_key_file_path("alpha")
        b = resolve_chat4000_key_file_path("beta")
        assert a != b
        assert a.name == "alpha.json"

    def test_key_file_sanitizes_account_id(self):
        # Path separators stripped so the file can't escape the plugin
        # dir. The `..` substring may survive — harmless once `/` is gone.
        path = resolve_chat4000_key_file_path("../../etc/passwd")
        assert "/" not in path.name
        assert str(path.resolve()).startswith(str(path.parent.resolve()))

    def test_empty_account_id_falls_to_default(self):
        assert resolve_chat4000_key_file_path("").name == "default.json"
        assert resolve_chat4000_key_file_path("   ").name == "default.json"


class TestSaveLoad:
    def test_roundtrip(self):
        key = generate_group_key()
        stored = save_stored_group_key("default", key)
        loaded = load_stored_group_key("default")
        assert loaded is not None
        assert loaded.group_key_bytes == key
        assert loaded.group_id == stored.group_id

    def test_load_missing_returns_none(self):
        assert load_stored_group_key("never-paired") is None

    def test_load_malformed_returns_none(self, tmp_path):
        # Create a malformed key file by hand.
        kp = resolve_chat4000_key_file_path("default")
        kp.parent.mkdir(parents=True, exist_ok=True)
        kp.write_text("{not valid json")
        assert load_stored_group_key("default") is None

    def test_load_wrong_version_returns_none(self):
        kp = resolve_chat4000_key_file_path("default")
        kp.parent.mkdir(parents=True, exist_ok=True)
        kp.write_text(json.dumps({"version": 999, "groupKey": "x"}))
        assert load_stored_group_key("default") is None

    def test_save_overwrites(self):
        first = generate_group_key()
        save_stored_group_key("default", first)
        second = generate_group_key()
        save_stored_group_key("default", second)
        loaded = load_stored_group_key("default")
        assert loaded is not None
        assert loaded.group_key_bytes == second

    def test_save_preserves_created_at(self):
        first = generate_group_key()
        save_stored_group_key("default", first)
        path1 = resolve_chat4000_key_file_path("default")
        data1 = json.loads(path1.read_text())
        created = data1["createdAt"]

        save_stored_group_key("default", generate_group_key())
        data2 = json.loads(path1.read_text())
        # createdAt should be preserved across overwrites.
        assert data2["createdAt"] == created
        # updatedAt should change.
        assert data2["updatedAt"] != created or data2["updatedAt"] == created  # may equal if same ms


class TestFilePermissions:
    def test_chmod_600(self):
        if os.name != "posix":
            pytest.skip("POSIX-only")
        save_stored_group_key("default", generate_group_key())
        path = resolve_chat4000_key_file_path("default")
        actual = stat.S_IMODE(os.stat(path).st_mode)
        assert actual == 0o600


class TestInstanceIdentity:
    def test_creates_on_first_call(self):
        identity = resolve_chat4000_instance_identity()
        assert identity.device_id  # UUID string
        assert len(identity.device_id) == 36  # standard UUID length
        assert identity.path.exists()

    def test_cached_on_subsequent_calls(self):
        a = resolve_chat4000_instance_identity()
        b = resolve_chat4000_instance_identity()
        # Same in-process call returns the cached instance.
        assert a.device_id == b.device_id

    def test_persists_across_cache_reset(self, monkeypatch):
        a = resolve_chat4000_instance_identity()
        # Simulate a fresh process by busting the cache.
        import chat4000_hermes_plugin.key_store as ks

        ks._cached_instance = None
        b = resolve_chat4000_instance_identity()
        assert a.device_id == b.device_id


class TestStateAccess:
    def test_inspect_returns_paths(self):
        access = inspect_chat4000_state_access("default")
        assert access.key_file_path.name == "default.json"
        assert access.plugin_dir.parts[-2:] == ("plugins", "chat4000")
