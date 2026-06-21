"""Durable sliding-sync cursor store (protocol D, "Sync cursor & key delivery").

The plugin is a WS device and MUST persist BOTH sliding-sync cursors — the room
cursor (`pos`) and the to-device cursor (`to_device_pos`) — to durable storage and
replay them on every start, so a *process restart* resumes an INCREMENTAL sync.
A restart that begins a fresh, cursor-less sync silently drops the
`device_lists.changed` delta (the signal that announces a newly paired device) and
is a conformance violation of D.

Mirrors what OpenClaw already does (and has a passing test for): a small file
under the plugin's per-account state dir holding `{pos, to_device_pos}`, written
ATOMICALLY (temp file + rename) so a crash mid-write can never leave a corrupt or
half-written cursor file — the two cursors always land together.

Ordering (D, "ack-only-after-durable"): the to-device cursor is persisted here
ONLY on the gateway ack path, which the crypto driver reaches AFTER it has flushed
the room keys for that frame to the crypto store (`receive_sync_changes` →
`ack_sync`). So this file never records a `to_device_pos` ahead of the keys it
acknowledges; the homeserver is never told to delete keys the store hasn't saved.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from ..key_store import resolve_chat4000_plugin_dir

logger = logging.getLogger(__name__)


def cursor_store_path(account_id: str = "default") -> Path:
    """Where this account's two-cursor file lives — alongside the bot creds and
    crypto store under the per-account plugin dir."""
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in (account_id or "default"))
    return resolve_chat4000_plugin_dir() / f"sync-cursors-{safe}.json"


@dataclass
class PersistedCursors:
    pos: str | None = None
    to_device_pos: str | None = None


class CursorStore:
    """File-backed durable home for the two sliding-sync cursors of one account.

    `load()` is called once at construction (so a fresh process resumes from the
    last durably-acked position); `persist()` is called on every gateway ack with
    whatever cursors were just durably saved (the room cursor always, the to-device
    cursor carried forward), and writes atomically.
    """

    def __init__(self, account_id: str = "default") -> None:
        self._path = cursor_store_path(account_id)

    def load(self) -> PersistedCursors:
        """Read the durably-persisted cursors. Missing / unreadable / malformed →
        a fresh (cursor-less) sync, which is the correct fail-safe: the homeserver
        re-delivers undeleted to-device, so worst case is a full initial sync."""
        if not self._path.exists():
            return PersistedCursors()
        try:
            raw = self._path.read_text(encoding="utf-8").strip()
        except OSError:
            return PersistedCursors()
        if not raw:
            return PersistedCursors()
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return PersistedCursors()
        if not isinstance(parsed, dict):
            return PersistedCursors()
        pos = parsed.get("pos")
        td = parsed.get("to_device_pos")
        return PersistedCursors(
            pos=pos if isinstance(pos, str) else None,
            to_device_pos=td if isinstance(td, str) else None,
        )

    def clear_cursors(self, names: list[str]) -> None:
        """Discard exactly the named durable cursor(s), leaving every other cursor
        intact (protocol D.1 `sync_reset` / D.2 "Device rule"). On a `pos_expired`
        reset the gateway sends `cursors: ["pos"]`, so this clears the room `pos`
        only and KEEPS `to_device_pos` — the to-device stream is a separate, durable
        token the homeserver never invalidates, and dropping it would lose Megolm
        keys.

        We re-read the current file, drop only the named keys, and atomically rewrite
        the remainder — so a later reconnect cannot replay the discarded cursor while
        the surviving one(s) stay valid. Unknown names are ignored (a cursor we never
        persisted is already "absent"). Best-effort, like `persist()`: a failed clear
        is logged, never raised — the in-memory reset (gateway_client) is what keeps
        the live socket correct; the file only matters across a process restart, and
        the worst case there is one extra homeserver `M_UNKNOWN_POS` round-trip."""
        current = self.load()
        keep: dict[str, str] = {}
        if "pos" not in names and current.pos is not None:
            keep["pos"] = current.pos
        if "to_device_pos" not in names and current.to_device_pos is not None:
            keep["to_device_pos"] = current.to_device_pos
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(keep), encoding="utf-8")
            with contextlib.suppress(OSError):
                os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)
        except OSError as exc:
            logger.warning("sync cursor clear failed (%s): %s", self._path, exc)

    def persist(self, pos: str | None, to_device_pos: str | None) -> None:
        """Atomically write both cursors as one object (temp file + rename) so the
        two always land together and a crash can't corrupt the file. Best-effort:
        a failed persist must never break the ack path (worst case a restart resyncs
        from the last good cursor), so we log and continue rather than raise."""
        payload: dict[str, str] = {}
        if pos is not None:
            payload["pos"] = pos
        if to_device_pos is not None:
            payload["to_device_pos"] = to_device_pos
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            with contextlib.suppress(OSError):
                os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)
        except OSError as exc:
            logger.warning("sync cursor persist failed (%s): %s", self._path, exc)
