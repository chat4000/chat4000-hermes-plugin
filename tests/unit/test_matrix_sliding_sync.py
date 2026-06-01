"""Sliding-sync request builder + frame parser (pure data-shaping)."""

from __future__ import annotations

from chat4000_hermes_plugin.matrix.sliding_sync import build_sync_request, parse_sync_frame


def test_build_request_enables_e2ee_and_to_device():
    req = build_sync_request(timeline_limit=10, window=50)
    assert req["lists"]["all"]["ranges"] == [[0, 49]]
    assert req["lists"]["all"]["timeline_limit"] == 10
    assert req["extensions"]["e2ee"]["enabled"] is True
    assert req["extensions"]["to_device"]["enabled"] is True


def test_parse_full_frame():
    frame = {
        "pos": "p7",
        "rooms": {"!r:hs": {"timeline": [{"type": "m.room.encrypted"}]}},
        "extensions": {
            "to_device": {"events": [{"type": "m.room.encrypted"}]},
            "e2ee": {
                "device_lists": {"changed": ["@u:hs"], "left": []},
                "device_one_time_keys_count": {"signed_curve25519": 42},
                "device_unused_fallback_key_types": ["signed_curve25519"],
            },
        },
    }
    p = parse_sync_frame(frame)
    assert p.pos == "p7"
    assert p.one_time_key_counts == {"signed_curve25519": 42}
    assert p.device_lists == {"changed": ["@u:hs"], "left": []}
    assert p.unused_fallback_keys == ["signed_curve25519"]
    assert len(p.to_device_events) == 1
    assert "!r:hs" in p.rooms
    assert len(p.rooms["!r:hs"]["timeline"]) == 1


def test_parse_sparse_frame_defaults():
    # An idle poll carries only pos.
    p = parse_sync_frame({"pos": "p1"})
    assert p.pos == "p1"
    assert p.to_device_events == []
    assert p.device_lists == {"changed": [], "left": []}
    assert p.one_time_key_counts == {}
    assert p.unused_fallback_keys is None
    assert p.rooms == {}
