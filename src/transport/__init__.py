"""`MessageTransport` facade — the consumer-facing API that hides every
wire-protocol detail.

Port of clawconnect-plugin/src/transport/index.ts. The adapter and the
agent reply pipeline call only `send()` and observe three callbacks.
They never see `seq`, never call `recv_ack`, never open a socket.

Scope: session-time only. Pairing runs BEFORE the group key exists and
lives in `src/pairing.py`. Construct a `MessageTransport` only after
pairing has produced a stable group key."""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal, Optional, Union

from ..protocol_types import (
    ConnectionFailed,
    ConnectionState,
    InnerMessage,
    OutboundAck,
    OutboundAudio,
    OutboundImage,
    OutboundMessage,
    OutboundStatus,
    OutboundText,
    OutboundTextDelta,
    OutboundTextEnd,
    OutboundToolDelta,
    OutboundToolEnd,
    OutboundToolStart,
    StatusUpdate,
)


# Connection state can be either a literal or a failed-with-reason dict.
ConnectionStateUnion = Union[ConnectionState, ConnectionFailed]


@dataclass
class GroupConfig:
    """Everything the transport needs to connect. Built by the adapter
    from the resolved Chat4000Account."""

    account_id: str
    group_id: str
    group_key_bytes: bytes
    relay_url: Optional[str] = None
    release_channel: Optional[str] = "production"
    runtime_log_level: Literal["info", "debug"] = "info"


Unsubscribe = Callable[[], None]
ReceiveHandler = Callable[[InnerMessage], Awaitable[None] | None]
StatusHandler = Callable[[StatusUpdate], Awaitable[None] | None]
ConnectionHandler = Callable[[ConnectionStateUnion], Awaitable[None] | None]


class MessageTransport(abc.ABC):
    """Abstract base. Real impl is RelayMessageTransport; tests use
    MockMessageTransport.

    Invariants the default impl enforces:
      - on_receive fires once per inner.id (§6.6.9 dedup)
      - at most one outbound ack per (refs, stage)
      - at most one outbound text_end per stream_id
      - one tool_end per tool_id
      - each text_delta/text_end/tool_* frame gets a fresh inner.id
      - notify_if_offline=True only on text/image/audio/non-reset textEnd
      - never exposes seq or outer envelope to consumers
    """

    @abc.abstractmethod
    def send(self, msg: OutboundMessage) -> str: ...

    @abc.abstractmethod
    def on_receive(self, handler: ReceiveHandler) -> Unsubscribe: ...

    @abc.abstractmethod
    def on_status(self, handler: StatusHandler) -> Unsubscribe: ...

    @abc.abstractmethod
    def on_connection_state(
        self, handler: ConnectionHandler
    ) -> Unsubscribe: ...

    @abc.abstractmethod
    def connect(self, config: GroupConfig) -> None: ...

    @abc.abstractmethod
    async def disconnect(self) -> None: ...
