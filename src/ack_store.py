"""Persistent ack/dedup state for the §6.6 reliable-delivery layer.

Port of clawconnect-plugin/src/ack-store.ts. Uses the Python stdlib's
sqlite3 module — Python ships with a native SQLite binary, so we avoid
the WASM wrapper the TS plugin needed for node-sqlite3-wasm.

Tables:
  - meta(group_id, role, last_acked_seq) — high-water mark for §6.6.8
    reconnect replay. Monotonic; never moves backwards.
  - processed_msg_ids(group_id, inner_msg_id) — application-layer dedup
    per §6.6.9. Survives plugin restart so a relay redrive of a msg we
    already handed to the agent is re-ack'd but NOT re-dispatched.
  - inner_acks(group_id, refs, stage) — Flow B idempotency. We emit
    at most one inner-ack per (refs, stage) across restarts.

Storage: ~/.hermes/plugins/chat4000/state/<account>.sqlite (chmod 600).
WAL + synchronous=FULL — every commit fsyncs the journal before touching
the main file. ACID across crashes.

Stale-lock recovery: SQLite's locking primitive on POSIX is fcntl()-based
and self-clears on process death, so the elaborate stale-lock-dir cleanup
the TS impl needs (because node-sqlite3-wasm uses mkdir-locks) is not
needed here. We keep a stub function for API parity with the TS plugin.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .key_store import resolve_chat4000_plugin_dir

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
  group_id TEXT NOT NULL,
  role TEXT NOT NULL,
  last_acked_seq INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (group_id, role)
);

CREATE TABLE IF NOT EXISTS processed_msg_ids (
  group_id TEXT NOT NULL,
  inner_msg_id TEXT NOT NULL,
  persisted_at INTEGER NOT NULL,
  PRIMARY KEY (group_id, inner_msg_id)
);

CREATE TABLE IF NOT EXISTS inner_acks (
  group_id TEXT NOT NULL,
  refs TEXT NOT NULL,
  stage TEXT NOT NULL,
  emitted_at INTEGER NOT NULL,
  PRIMARY KEY (group_id, refs, stage)
);
"""


def _sanitize_account_id(account_id: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9._-]", "_", (account_id or "").strip() or "default")


def _resolve_state_dir() -> Path:
    return resolve_chat4000_plugin_dir() / "state"


def resolve_ack_store_path(account_id: str) -> Path:
    return _resolve_state_dir() / f"{_sanitize_account_id(account_id)}.sqlite"


def cleanup_stale_ack_store_lock(db_path: Path) -> bool:
    """No-op on Python (sqlite3 uses fcntl/POSIX advisory locks that
    self-clear on process death). Kept for API parity with the TS impl
    so callers don't branch on platform."""
    return False


@dataclass
class MarkProcessedResult:
    is_new: bool


@dataclass
class MarkInnerAckResult:
    is_new: bool


class Chat4000AckStore:
    """Per-account SQLite store. Thread-safe under sqlite3's
    `check_same_thread=False` + a single connection per instance.

    Holds three statement caches. Statements are prepared lazily on first
    use to keep open() fast — the gateway opens many of these in series
    when scanning multi-account configs."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            isolation_level=None,  # autocommit; we use explicit BEGIN where needed
        )
        # WAL gives concurrent readers while writes are in flight.
        # synchronous=FULL fsyncs the journal on every commit (§8.1).
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = FULL")
        self._conn.executescript(_SCHEMA_SQL)

        # chmod 600 — the dedup table doesn't contain plaintext but does
        # contain msg_ids that link to relay traffic; tighten anyway.
        try:
            current_mode = stat.S_IMODE(os.stat(self.db_path).st_mode)
            if current_mode != 0o600:
                os.chmod(self.db_path, 0o600)
        except OSError:
            pass

    # ─── Flow A high-water mark ───────────────────────────────────────────

    def get_last_acked_seq(self, group_id: str, role: str = "plugin") -> int:
        cur = self._conn.execute(
            "SELECT last_acked_seq FROM meta WHERE group_id = ? AND role = ?",
            (group_id, role),
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0

    def set_last_acked_seq(
        self, group_id: str, seq: int, role: str = "plugin"
    ) -> None:
        """Monotonic — MAX() against the existing row keeps redundant lower
        writes from regressing the watermark (defensive; the recv-ack
        batcher already gates this client-side)."""
        if seq < 0:
            return
        self._conn.execute(
            """INSERT INTO meta (group_id, role, last_acked_seq, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(group_id, role) DO UPDATE SET
                 last_acked_seq = MAX(meta.last_acked_seq, excluded.last_acked_seq),
                 updated_at = excluded.updated_at""",
            (group_id, role, int(seq), int(time.time() * 1000)),
        )

    # ─── Application-layer dedup by inner.id (§6.6.9) ─────────────────────

    def mark_processed(self, group_id: str, inner_msg_id: str) -> MarkProcessedResult:
        cur = self._conn.execute(
            """INSERT OR IGNORE INTO processed_msg_ids
               (group_id, inner_msg_id, persisted_at)
               VALUES (?, ?, ?)""",
            (group_id, inner_msg_id, int(time.time() * 1000)),
        )
        return MarkProcessedResult(is_new=cur.rowcount == 1)

    def is_processed(self, group_id: str, inner_msg_id: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM processed_msg_ids WHERE group_id = ? AND inner_msg_id = ? LIMIT 1",
            (group_id, inner_msg_id),
        )
        return cur.fetchone() is not None

    # ─── Inner-ack idempotency (Flow B) ───────────────────────────────────

    def mark_inner_ack_emitted(
        self, *, group_id: str, refs: str, stage: str
    ) -> MarkInnerAckResult:
        cur = self._conn.execute(
            """INSERT OR IGNORE INTO inner_acks (group_id, refs, stage, emitted_at)
               VALUES (?, ?, ?, ?)""",
            (group_id, refs, stage, int(time.time() * 1000)),
        )
        return MarkInnerAckResult(is_new=cur.rowcount == 1)

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass


# ─── Process-wide cache so multiple connect() calls share one DB handle ────

_cache: dict[str, Chat4000AckStore] = {}


def open_ack_store(account_id: str) -> Chat4000AckStore:
    db_path = resolve_ack_store_path(account_id)
    key = str(db_path)
    cached = _cache.get(key)
    if cached is not None:
        return cached
    store = Chat4000AckStore(db_path)
    _cache[key] = store
    return store


def _reset_ack_store_cache_for_tests() -> None:
    """Test-only: close all cached stores so the next open() makes fresh
    instances. Used by tests that swap in tmp_path-based DB paths."""
    for store in list(_cache.values()):
        store.close()
    _cache.clear()
