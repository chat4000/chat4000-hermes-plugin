"""Wire-format types: to_wire dict shape, snake_case keys, None-field drop."""

from __future__ import annotations

import time

from chat4000_hermes_plugin.protocol_types import (
    InnerMessage,
    InnerMessageFrom,
    OutboundToolDelta,
    OutboundToolEnd,
    OutboundToolStart,
)


class TestInnerMessageToWire:
    def test_basic_shape(self):
        inner = InnerMessage(
            t="text",
            id="abc123",
            from_=InnerMessageFrom(role="plugin"),
            body={"text": "hello"},
            ts=1700000000000,
        )
        wire = inner.to_wire()
        assert wire["t"] == "text"
        assert wire["id"] == "abc123"
        assert wire["body"] == {"text": "hello"}
        assert wire["ts"] == 1700000000000
        assert wire["from"] == {"role": "plugin"}

    def test_from_none_omitted(self):
        inner = InnerMessage(
            t="text", id="x", from_=None, body={"text": "hi"}, ts=1
        )
        wire = inner.to_wire()
        assert "from" not in wire

    def test_from_drops_none_fields(self):
        inner = InnerMessage(
            t="text",
            id="x",
            from_=InnerMessageFrom(
                role="plugin",
                device_id="dev-1",
                device_name=None,  # gets dropped
                app_version="1.0.0",
                bundle_id=None,  # gets dropped
            ),
            body={"text": "x"},
            ts=1,
        )
        wire = inner.to_wire()
        assert wire["from"] == {
            "role": "plugin",
            "device_id": "dev-1",
            "app_version": "1.0.0",
        }


class TestToolFrames:
    def test_tool_start_has_correct_kind(self):
        s = OutboundToolStart(tool_id="t1", name="bash", args="ls")
        # The discriminator field — distinct from text/image/audio/etc.
        assert s.kind == "toolStart"
        assert s.tool_id == "t1"
        assert s.name == "bash"

    def test_tool_delta_kind(self):
        d = OutboundToolDelta(tool_id="t1", delta="output line\n")
        assert d.kind == "toolDelta"

    def test_tool_end_kind(self):
        e = OutboundToolEnd(tool_id="t1", status="done", result="ok", duration_ms=42)
        assert e.kind == "toolEnd"
        assert e.duration_ms == 42
