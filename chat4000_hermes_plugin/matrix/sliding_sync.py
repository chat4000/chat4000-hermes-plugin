"""Sliding sync (MSC4186 / simplified) — request builder + frame parser.

The gateway runs the sliding-sync long-poll and pushes `sync` frames with
`pos`/`rooms`/`extensions` at the top level (see the gateway's `sync_frame`).
This module is pure data-shaping: build the request body the plugin sends in
`sync_start`, and pull a `sync` frame apart into the pieces the crypto driver and
room layer consume. No I/O, no crypto — unit-testable in isolation.

⚠️ Version caveat (pushback note): matrix-rust-sdk / Synapse / Tuwunel pin the
exact `required_state` sentinels (`$ME`, `$LAZY`) and the e2ee/to_device
extension shape across MSC4186 revisions. Keep this builder and the Tuwunel fork
version-locked and re-test on every bump.
"""

from __future__ import annotations

from dataclasses import dataclass, field


def build_sync_request(*, timeline_limit: int = 20, window: int = 100) -> dict:
    """The sliding-sync request body the plugin sends in `sync_start`.

    One list covering all the plugin's rooms (control + sessions), with the e2ee
    and to_device extensions enabled — the to_device extension is how Olm-encrypted
    Megolm room keys arrive, and e2ee carries device lists + one-time-key counts.
    """
    return {
        "lists": {
            "all": {
                "ranges": [[0, max(0, window - 1)]],
                # `$LAZY` = lazy-load membership; we need room_kind + encryption
                # state + members. Keep explicit types small to bound the frame.
                "required_state": [
                    ["m.room.encryption", ""],
                    ["m.room.member", "$LAZY"],
                    ["chat4000.room_kind", ""],
                    ["m.room.name", ""],
                    ["m.space.child", "*"],
                ],
                "timeline_limit": timeline_limit,
            }
        },
        "extensions": {
            "to_device": {"enabled": True},
            "e2ee": {"enabled": True},
            "account_data": {"enabled": True},
        },
    }


@dataclass
class ParsedSync:
    """A `sync` frame pulled apart for the crypto driver + room layer."""

    pos: str | None
    to_device_events: list[dict] = field(default_factory=list)
    device_lists: dict[str, list[str]] = field(default_factory=lambda: {"changed": [], "left": []})
    one_time_key_counts: dict[str, int] = field(default_factory=dict)
    unused_fallback_keys: list[str] | None = None
    # room_id → {"timeline": [events], "required_state": [events]}
    rooms: dict[str, dict] = field(default_factory=dict)


def parse_sync_frame(frame: dict) -> ParsedSync:
    """Pull a gateway `sync` frame into its crypto-relevant + room parts.

    Defensive against absent sections — early syncs and idle polls carry only
    `pos`. The e2ee extension nests device lists + OTK counts; to_device nests
    the encrypted room-key events."""
    pos = frame.get("pos")
    ext = frame.get("extensions") or {}

    to_device = ((ext.get("to_device") or {}).get("events")) or []

    e2ee = ext.get("e2ee") or {}
    dl = e2ee.get("device_lists") or {}
    device_lists = {
        "changed": list(dl.get("changed") or []),
        "left": list(dl.get("left") or []),
    }
    otk = e2ee.get("device_one_time_keys_count") or {}
    one_time_key_counts = {str(k): int(v) for k, v in otk.items()}
    fallback = e2ee.get("device_unused_fallback_key_types")
    unused_fallback = list(fallback) if fallback is not None else None

    rooms_in = frame.get("rooms") or {}
    rooms: dict[str, dict] = {}
    for room_id, r in rooms_in.items():
        if not isinstance(r, dict):
            continue
        rooms[room_id] = {
            "timeline": list(r.get("timeline") or []),
            "required_state": list(r.get("required_state") or []),
        }

    return ParsedSync(
        pos=pos,
        to_device_events=list(to_device),
        device_lists=device_lists,
        one_time_key_counts=one_time_key_counts,
        unused_fallback_keys=unused_fallback,
        rooms=rooms,
    )


def extract_membership(room: dict) -> dict[str, str]:
    """Pull `m.room.member` events from one room's `required_state` + `timeline`
    into a `{mxid: membership}` map (latest-wins; timeline overrides the state
    snapshot). `membership` is one of join|invite|leave|ban|knock.

    This is how the plugin learns who is ACTUALLY in a room — the recipient set
    for Megolm key sharing. With `$LAZY` membership the snapshot only carries the
    members relevant to the timeline (e.g. anyone who spoke), which is exactly the
    users we must share keys with."""
    out: dict[str, str] = {}
    for ev in list(room.get("required_state") or []) + list(room.get("timeline") or []):
        if ev.get("type") != "m.room.member":
            continue
        mxid = ev.get("state_key")
        membership = (ev.get("content") or {}).get("membership")
        if mxid and membership:
            out[mxid] = membership
    return out
