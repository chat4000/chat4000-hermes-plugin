"""In-memory `MessageTransport` for tests.

Same enforcement as the real impl: dedup on inner.id, ack idempotency,
single text_end per stream_id, single tool_end per tool_id."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional

from ..types import (
    InnerMessage,
    OutboundAck,
    OutboundMessage,
    OutboundTextEnd,
    OutboundToolEnd,
    StatusUpdate,
)
from . import (
    ConnectionHandler,
    ConnectionStateUnion,
    GroupConfig,
    MessageTransport,
    ReceiveHandler,
    StatusHandler,
    Unsubscribe,
)


@dataclass
class SentMessage:
    wire_id: str
    message: OutboundMessage


class MockMessageTransport(MessageTransport):
    def __init__(self) -> None:
        self.sent: list[SentMessage] = []
        self.last_config: Optional[GroupConfig] = None
        self._receive_handlers: set[ReceiveHandler] = set()
        self._status_handlers: set[StatusHandler] = set()
        self._state_handlers: set[ConnectionHandler] = set()
        self._connection_state: ConnectionStateUnion = "disconnected"
        self._disposed = False
        self._inner_ids_seen: set[str] = set()
        self._acks_emitted: set[str] = set()
        self._stream_ended_wire_id: dict[str, str] = {}
        self._tool_ended_wire_id: dict[str, str] = {}

    # ─── MessageTransport surface ────────────────────────────────────────

    def send(self, msg: OutboundMessage) -> str:
        if self._disposed:
            raise RuntimeError("MockMessageTransport: send() after disconnect()")

        if isinstance(msg, OutboundAck):
            key = f"{msg.refs}::{msg.stage}"
            existing = self._find_existing_ack(msg.refs, msg.stage)
            if existing is not None:
                return existing
            wire_id = str(uuid.uuid4())
            self._acks_emitted.add(key)
            self.sent.append(SentMessage(wire_id=wire_id, message=msg))
            return wire_id

        if isinstance(msg, OutboundTextEnd):
            cached = self._stream_ended_wire_id.get(msg.stream_id)
            if cached is not None:
                return cached
            wire_id = str(uuid.uuid4())
            self._stream_ended_wire_id[msg.stream_id] = wire_id
            self.sent.append(SentMessage(wire_id=wire_id, message=msg))
            return wire_id

        if isinstance(msg, OutboundToolEnd):
            cached = self._tool_ended_wire_id.get(msg.tool_id)
            if cached is not None:
                return cached
            wire_id = str(uuid.uuid4())
            self._tool_ended_wire_id[msg.tool_id] = wire_id
            self.sent.append(SentMessage(wire_id=wire_id, message=msg))
            return wire_id

        wire_id = str(uuid.uuid4())
        self.sent.append(SentMessage(wire_id=wire_id, message=msg))
        return wire_id

    def on_receive(self, handler: ReceiveHandler) -> Unsubscribe:
        self._receive_handlers.add(handler)
        return lambda: self._receive_handlers.discard(handler)

    def on_status(self, handler: StatusHandler) -> Unsubscribe:
        self._status_handlers.add(handler)
        return lambda: self._status_handlers.discard(handler)

    def on_connection_state(self, handler: ConnectionHandler) -> Unsubscribe:
        self._state_handlers.add(handler)
        handler(self._connection_state)
        return lambda: self._state_handlers.discard(handler)

    def connect(self, config: GroupConfig) -> None:
        self.last_config = config
        self.simulate_state("connecting")
        self.simulate_state("connected")

    async def disconnect(self) -> None:
        self.simulate_state("disconnected")
        self._disposed = True

    # ─── Test driver API ─────────────────────────────────────────────────

    def simulate_receive(self, msg: InnerMessage) -> None:
        if msg.id in self._inner_ids_seen:
            return
        self._inner_ids_seen.add(msg.id)
        for handler in list(self._receive_handlers):
            handler(msg)

    def simulate_receive_unchecked(self, msg: InnerMessage) -> None:
        for handler in list(self._receive_handlers):
            handler(msg)

    def simulate_status(self, update: StatusUpdate) -> None:
        for handler in list(self._status_handlers):
            handler(update)

    def simulate_state(self, state: ConnectionStateUnion) -> None:
        self._connection_state = state
        for handler in list(self._state_handlers):
            handler(state)

    def reset(self) -> None:
        self.sent.clear()
        self._inner_ids_seen.clear()
        self._acks_emitted.clear()
        self._stream_ended_wire_id.clear()
        self._tool_ended_wire_id.clear()

    def _find_existing_ack(self, refs: str, stage: str) -> Optional[str]:
        for entry in reversed(self.sent):
            if isinstance(entry.message, OutboundAck):
                if entry.message.refs == refs and entry.message.stage == stage:
                    return entry.wire_id
        return None
