"""SQLite ack store: watermark monotonicity, msg_id dedup,
inner-ack idempotency, persistence across close/reopen, file perms.

These tests pin the protocol §6.6 / §6.6.9 / §8.1 contracts that the
durable store provides for the relay's reliable-delivery layer."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from src.ack_store import (
    Chat4000AckStore,
    _reset_ack_store_cache_for_tests,
    cleanup_stale_ack_store_lock,
    open_ack_store,
    resolve_ack_store_path,
)


@pytest.fixture
def store(tmp_path):
    """Fresh store at a tmp_path-scoped DB."""
    db_path = tmp_path / "test.sqlite"
    s = Chat4000AckStore(db_path)
    yield s
    s.close()


class TestWatermark:
    def test_initial_zero(self, store):
        assert store.get_last_acked_seq("g1") == 0

    def test_set_and_get(self, store):
        store.set_last_acked_seq("g1", 42)
        assert store.get_last_acked_seq("g1") == 42

    def test_monotonic(self, store):
        store.set_last_acked_seq("g1", 100)
        # Lower seq must NOT regress the watermark — relay's offline-queue
        # eviction relies on cumulative high-water mark.
        store.set_last_acked_seq("g1", 50)
        assert store.get_last_acked_seq("g1") == 100

    def test_per_group_isolation(self, store):
        store.set_last_acked_seq("g1", 10)
        store.set_last_acked_seq("g2", 20)
        assert store.get_last_acked_seq("g1") == 10
        assert store.get_last_acked_seq("g2") == 20

    def test_per_role_isolation(self, store):
        store.set_last_acked_seq("g1", 10, role="plugin")
        store.set_last_acked_seq("g1", 99, role="app")
        assert store.get_last_acked_seq("g1", role="plugin") == 10
        assert store.get_last_acked_seq("g1", role="app") == 99

    def test_negative_seq_rejected(self, store):
        store.set_last_acked_seq("g1", 10)
        store.set_last_acked_seq("g1", -5)
        assert store.get_last_acked_seq("g1") == 10


class TestProcessedMsgIds:
    """§6.6.9 — dedup on inner.id, not outer seq."""

    def test_mark_processed_is_new_first_time(self, store):
        result = store.mark_processed("g1", "msg-abc")
        assert result.is_new is True

    def test_mark_processed_idempotent(self, store):
        first = store.mark_processed("g1", "msg-abc")
        second = store.mark_processed("g1", "msg-abc")
        assert first.is_new is True
        assert second.is_new is False

    def test_is_processed_reflects_state(self, store):
        assert store.is_processed("g1", "msg-abc") is False
        store.mark_processed("g1", "msg-abc")
        assert store.is_processed("g1", "msg-abc") is True

    def test_dedup_per_group(self, store):
        store.mark_processed("g1", "msg-abc")
        # Same msg_id in a different group is allowed — different groups
        # have independent dedup tables.
        result = store.mark_processed("g2", "msg-abc")
        assert result.is_new is True


class TestInnerAckIdempotency:
    """Flow B inner ack — one emission per (refs, stage)."""

    def test_first_emit_is_new(self, store):
        r = store.mark_inner_ack_emitted(group_id="g1", refs="m1", stage="received")
        assert r.is_new is True

    def test_second_emit_not_new(self, store):
        store.mark_inner_ack_emitted(group_id="g1", refs="m1", stage="received")
        r = store.mark_inner_ack_emitted(group_id="g1", refs="m1", stage="received")
        assert r.is_new is False

    def test_different_stage_is_new(self, store):
        store.mark_inner_ack_emitted(group_id="g1", refs="m1", stage="received")
        # `processing` and `displayed` stages are independent entries.
        r = store.mark_inner_ack_emitted(group_id="g1", refs="m1", stage="processing")
        assert r.is_new is True

    def test_different_refs_is_new(self, store):
        store.mark_inner_ack_emitted(group_id="g1", refs="m1", stage="received")
        r = store.mark_inner_ack_emitted(group_id="g1", refs="m2", stage="received")
        assert r.is_new is True

    def test_different_group_is_new(self, store):
        store.mark_inner_ack_emitted(group_id="g1", refs="m1", stage="received")
        r = store.mark_inner_ack_emitted(group_id="g2", refs="m1", stage="received")
        assert r.is_new is True


class TestPersistence:
    def test_survives_close_reopen(self, tmp_path):
        db_path = tmp_path / "persist.sqlite"
        store = Chat4000AckStore(db_path)
        store.set_last_acked_seq("g1", 42)
        store.mark_processed("g1", "m1")
        store.mark_inner_ack_emitted(group_id="g1", refs="m1", stage="received")
        store.close()

        store2 = Chat4000AckStore(db_path)
        assert store2.get_last_acked_seq("g1") == 42
        assert store2.is_processed("g1", "m1") is True
        # Inner-ack idempotency persists too.
        r = store2.mark_inner_ack_emitted(group_id="g1", refs="m1", stage="received")
        assert r.is_new is False
        store2.close()

    def test_wal_pragma_set(self, store):
        """WAL journal mode is required for safe concurrent reads with
        in-flight writes (§8.1)."""
        cur = store._conn.execute("PRAGMA journal_mode")
        mode = cur.fetchone()[0]
        assert mode.lower() == "wal"

    def test_synchronous_full_pragma_set(self, store):
        """synchronous=FULL means each commit fsyncs the journal — the
        crash-safety promise of §8.1."""
        cur = store._conn.execute("PRAGMA synchronous")
        # `synchronous=FULL` reports as integer 2.
        assert cur.fetchone()[0] == 2


class TestFilePermissions:
    def test_chmod_600_on_open(self, tmp_path):
        db_path = tmp_path / "perms.sqlite"
        store = Chat4000AckStore(db_path)
        store.close()
        if os.name != "posix":
            pytest.skip("file modes are POSIX-only")
        actual = stat.S_IMODE(os.stat(db_path).st_mode)
        assert actual == 0o600

    def test_fixes_wrong_perms_on_reopen(self, tmp_path):
        if os.name != "posix":
            pytest.skip("file modes are POSIX-only")
        db_path = tmp_path / "perms2.sqlite"
        store = Chat4000AckStore(db_path)
        store.close()
        os.chmod(db_path, 0o644)
        store2 = Chat4000AckStore(db_path)
        store2.close()
        assert stat.S_IMODE(os.stat(db_path).st_mode) == 0o600


class TestCleanupStaleLock:
    def test_returns_false_on_posix(self, tmp_path):
        """sqlite3 uses fcntl advisory locks that self-clear on process
        death, so the mkdir-lock cleanup the TS impl needs is a no-op
        here. Kept for API parity."""
        assert cleanup_stale_ack_store_lock(tmp_path / "x.sqlite") is False


class TestModuleLevelHelpers:
    def test_open_ack_store_caches_by_path(self):
        a = open_ack_store("alpha")
        b = open_ack_store("alpha")
        assert a is b
        _reset_ack_store_cache_for_tests()

    def test_open_ack_store_different_accounts(self):
        a = open_ack_store("alpha")
        b = open_ack_store("beta")
        assert a is not b
        _reset_ack_store_cache_for_tests()

    def test_resolve_path_sanitizes_account_id(self):
        # Path-traversal attempt → sanitized: separators replaced with `_`
        # so the file can't escape the plugin dir. (The substring `..`
        # may survive in the name itself — harmless once `/` is gone.)
        path = resolve_ack_store_path("../../etc/passwd")
        assert "/" not in path.name
        assert "_" in path.name
        assert str(path.resolve()).startswith(str(path.parent.resolve()))

    def test_resolve_path_empty_account_falls_back_to_default(self):
        assert resolve_ack_store_path("").name == "default.sqlite"
        assert resolve_ack_store_path("   ").name == "default.sqlite"
