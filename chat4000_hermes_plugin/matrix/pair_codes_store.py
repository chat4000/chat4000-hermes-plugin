"""Outstanding pairing-codes store — the completion listener's persistent state.

Protocol C.4 "Completion listening": the gateway-resident plugin owns pairing
completion and must poll `GET /codes/{code}` for EVERY outstanding code it has
registered — including reusable ones — for the code's whole lifetime, SURVIVING
its own restarts. This store is that durable state: every registered code
(`chat4000 pair`, the installer, `device.pair_start`) is recorded here and the
resident listener (`pair_listener.py`) polls everything in it until the code
settles (single-use: redeemed/expired; reusable: expired only).

Writers: the CLI pair flow and the control-room command handler (both append at
register time); the listener updates `redeemed_count_seen` as redeems land and
removes settled codes. Like the sibling stores, decoupling the writer (CLI
process) from the poller (gateway process) means the CLI never needs the
gateway socket — the listener picks new records up by re-reading the file.

Stored at ~/.hermes/plugins/chat4000/pending-codes-<account>.json (mode 0600).
"""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from ..key_store import resolve_chat4000_plugin_dir


@dataclass
class PendingCode:
    """One outstanding pairing code the resident listener must watch."""

    code: str
    expires_at_ms: int  # unix ms, from the register response (0 = unknown)
    reusable: bool = False
    registered_at_ms: int = 0  # unix ms; drives the active-window poll cadence
    redeemed_count_seen: int = 0  # redeems already processed (device records)
    pair_id: str | None = None  # set for device.pair_start codes (E lifecycle)


def _path(account_id: str = "default") -> Path:
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in (account_id or "default"))
    return resolve_chat4000_plugin_dir() / f"pending-codes-{safe}.json"


def load_pending_codes(account_id: str = "default") -> list[PendingCode]:
    p = _path(account_id)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        records: list[PendingCode] = []
        for raw in data.get("codes", []):
            if not isinstance(raw, dict) or not raw.get("code"):
                continue
            records.append(
                PendingCode(
                    code=str(raw["code"]),
                    expires_at_ms=int(raw.get("expires_at_ms") or 0),
                    reusable=bool(raw.get("reusable", False)),
                    registered_at_ms=int(raw.get("registered_at_ms") or 0),
                    redeemed_count_seen=int(raw.get("redeemed_count_seen") or 0),
                    pair_id=str(raw["pair_id"]) if raw.get("pair_id") else None,
                )
            )
        return records
    except (OSError, json.JSONDecodeError, AttributeError, TypeError, ValueError):
        # Missing / unreadable / malformed store → nothing outstanding (callers
        # branch; a lost record only loses listener bookkeeping, never creds).
        return []


def add_pending_code(record: PendingCode, account_id: str = "default") -> None:
    """Record a freshly registered code (idempotent — replaces a same-code row)."""
    records = [r for r in load_pending_codes(account_id) if r.code != record.code]
    records.append(record)
    _save(records, account_id)


def update_pending_code(record: PendingCode, account_id: str = "default") -> None:
    """Persist listener progress (e.g. `redeemed_count_seen`) for one code."""
    add_pending_code(record, account_id)


def remove_pending_code(code: str, account_id: str = "default") -> None:
    records = load_pending_codes(account_id)
    kept = [r for r in records if r.code != code]
    if len(kept) != len(records):
        _save(kept, account_id)


def _save(records: list[PendingCode], account_id: str) -> None:
    p = _path(account_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({"codes": [asdict(r) for r in records]}, indent=2) + "\n", encoding="utf-8"
    )
    with contextlib.suppress(OSError):
        os.chmod(p, 0o600)
