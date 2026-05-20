"""MockMessageTransport — same invariants as the real transport for tests."""

from __future__ import annotations

import pytest

from chat4000_hermes_plugin.protocol_types import (
    InnerMessage,
    InnerMessageFrom,
    OutboundAck,
    OutboundText,
    OutboundTextDelta,
    OutboundTextEnd,
    OutboundToolEnd,
    OutboundToolStart,
    StatusUpdate,
)
from chat4000_hermes_plugin.transport import GroupConfig
from chat4000_hermes_plugin.transport.mock import MockMessageTransport


@pytest.fixture
def t():
    return MockMessageTransport()


class TestSendBasics:
    def test_text_send_recorded(self, t):
        wid = t.send(OutboundText(text="hello"))
        assert wid
        assert len(t.sent) == 1
        assert t.sent[0].wire_id == wid

    def test_status_send_recorded(self, t):
        from chat4000_hermes_plugin.protocol_types import OutboundStatus
        t.send(OutboundStatus(status="typing"))
        assert len(t.sent) == 1


class TestAckDedup:
    def test_same_refs_same_stage_dedupes(self, t):
        a = t.send(OutboundAck(refs="m1", stage="received"))
        b = t.send(OutboundAck(refs="m1", stage="received"))
        assert a == b
        # Only one wire frame went out.
        ack_count = sum(1 for s in t.sent if isinstance(s.message, OutboundAck))
        assert ack_count == 1

    def test_different_stage_not_dedupe(self, t):
        a = t.send(OutboundAck(refs="m1", stage="received"))
        b = t.send(OutboundAck(refs="m1", stage="displayed"))
        assert a != b
        ack_count = sum(1 for s in t.sent if isinstance(s.message, OutboundAck))
        assert ack_count == 2


class TestTextEndDedup:
    def test_same_stream_id_dedupes(self, t):
        a = t.send(OutboundTextEnd(stream_id="s1", text="hello"))
        b = t.send(OutboundTextEnd(stream_id="s1", text="world"))
        assert a == b


class TestToolEndDedup:
    """tool_end is also dedup'd by tool_id — matches the §6.4.2-style
    invariant for terminal frames."""

    def test_same_tool_id_dedupes(self, t):
        a = t.send(OutboundToolEnd(tool_id="x", status="done", result="ok", duration_ms=1))
        b = t.send(OutboundToolEnd(tool_id="x", status="failed", result="z", duration_ms=2))
        assert a == b


class TestInnerIdDedup:
    def test_simulate_receive_dedups_on_inner_id(self, t):
        received = []
        t.on_receive(lambda m: received.append(m))
        msg = InnerMessage(
            t="text",
            id="dup-id",
            from_=InnerMessageFrom(role="app"),
            body={"text": "hi"},
            ts=1,
        )
        t.simulate_receive(msg)
        t.simulate_receive(msg)
        assert len(received) == 1

    def test_simulate_receive_unchecked_bypasses_dedup(self, t):
        received = []
        t.on_receive(lambda m: received.append(m))
        msg = InnerMessage(t="text", id="x", from_=None, body={"text": "hi"}, ts=1)
        t.simulate_receive_unchecked(msg)
        t.simulate_receive_unchecked(msg)
        assert len(received) == 2


class TestHandlers:
    def test_on_status_fires(self, t):
        statuses = []
        t.on_status(lambda u: statuses.append(u))
        t.simulate_status(StatusUpdate(msg_id="m1", status="sent"))
        assert statuses == [StatusUpdate(msg_id="m1", status="sent")]

    def test_on_connection_state_fires_initial_state(self, t):
        states = []
        t.on_connection_state(lambda s: states.append(s))
        # The contract: handler is called with the current state immediately
        # on subscribe (so consumers don't miss an already-current state).
        assert states == ["disconnected"]

    def test_unsubscribe_stops_callbacks(self, t):
        received = []
        unsub = t.on_receive(lambda m: received.append(m))
        unsub()
        t.simulate_receive(InnerMessage(t="text", id="a", from_=None, body={}, ts=1))
        assert received == []


class TestLifecycle:
    def test_connect_simulates_state_progression(self, t):
        states = []
        t.on_connection_state(lambda s: states.append(s))
        cfg = GroupConfig(
            account_id="default",
            group_id="g1",
            group_key_bytes=b"\x00" * 32,
        )
        t.connect(cfg)
        # initial: disconnected → connecting → connected
        assert states == ["disconnected", "connecting", "connected"]
        assert t.last_config is cfg

    @pytest.mark.asyncio
    async def test_disconnect_blocks_further_sends(self, t):
        t.send(OutboundText(text="hi"))
        await t.disconnect()
        with pytest.raises(RuntimeError):
            t.send(OutboundText(text="late"))

    def test_reset_clears_history(self, t):
        t.send(OutboundText(text="hi"))
        t.simulate_receive(InnerMessage(t="text", id="x", from_=None, body={}, ts=1))
        t.reset()
        assert t.sent == []
        # After reset, simulate_receive of the same id is treated as fresh.
        received = []
        t.on_receive(lambda m: received.append(m))
        t.simulate_receive(InnerMessage(t="text", id="x", from_=None, body={}, ts=1))
        assert len(received) == 1
