"""Type definitions shared across the chat4000 Hermes plugin.

Mirrors the wire protocol from clawconnect-plugin/src/types.ts. All wire
fields use snake_case to match the relay's JSON spec; Python attributes
use snake_case naturally so the shapes line up without aliasing.

The protocol stays at version: 1 — new inner-message types (tool_start/
tool_delta/tool_end) added for Hermes tool-call streaming are additive
and ignored by older receivers per §6.6.9 dedup contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

# ─── Plugin config (loaded from Hermes platform config + env vars) ─────────

PairingLogLevel = Literal["info", "debug"]
RuntimeLogLevel = Literal["info", "debug"]
KeySource = Literal["state-file", "config", "env", "missing"]


@dataclass
class Chat4000AccountConfig:
    enabled: bool = True
    pairing_log_level: PairingLogLevel = "info"
    runtime_log_level: RuntimeLogLevel = "info"
    release_channel: str = "production"
    group_key: Optional[str] = None  # legacy/manual override
    text_chunk_limit: int = 4096
    block_streaming: bool = False


@dataclass
class Chat4000Config(Chat4000AccountConfig):
    accounts: dict[str, Chat4000AccountConfig] = field(default_factory=dict)
    default_account: Optional[str] = None


@dataclass
class ResolvedChat4000Account:
    account_id: str
    enabled: bool
    configured: bool
    relay_url: str
    pairing_log_level: PairingLogLevel
    runtime_log_level: RuntimeLogLevel
    group_id: str
    group_key_bytes: bytes
    key_file_path: str
    key_source: KeySource
    config: Chat4000AccountConfig


# ─── Relay wire envelopes (outer protocol) ─────────────────────────────────


@dataclass
class RelayEnvelope:
    version: int
    type: str
    payload: dict[str, Any]


@dataclass
class RelayHelloPayload:
    role: Literal["plugin"]
    group_id: str
    device_token: None  # plugins never have APNs tokens
    app_version: str
    release_channel: str
    last_acked_seq: Optional[int] = None


@dataclass
class RelayVersionPolicy:
    min_version: Optional[str] = None
    recommended_version: Optional[str] = None
    latest_version: Optional[str] = None


@dataclass
class RelayHelloOkPayload:
    current_terms_version: Optional[int] = None
    version_policy: Optional[RelayVersionPolicy] = None
    plugin_version_policy: Optional[RelayVersionPolicy] = None


@dataclass
class RelayMsgPayload:
    msg_id: str
    nonce: str
    ciphertext: str
    notify_if_offline: bool = False
    seq: Optional[int] = None


@dataclass
class RelayRecvAckPayload:
    up_to_seq: int
    ranges: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class RelayRecvSenderAckPayload:
    msg_id: str
    queued_for: list[str] = field(default_factory=list)


# ─── Pairing payloads ──────────────────────────────────────────────────────


@dataclass
class RelayWrappedKeyPayload:
    ephemeral_pub: str
    nonce: str
    ciphertext: str


@dataclass
class RelayPairOpenPayload:
    role: Literal["initiator", "joiner"]
    room_id: str


@dataclass
class PairingHello:
    t: Literal["hello"]
    salt: str


@dataclass
class PairingJoin:
    t: Literal["join"]
    salt: str


@dataclass
class PairingProofB:
    t: Literal["proof_b"]
    proof: str


@dataclass
class PairingGrant:
    t: Literal["grant"]
    proof: str
    wrapped_key: RelayWrappedKeyPayload


# ─── Inner (encrypted) message types ───────────────────────────────────────

InnerMessageType = Literal[
    "text",
    "image",
    "audio",
    "text_delta",
    "text_end",
    "status",
    "ack",
    # Hermes tool-call streaming — added 2026-05; backwards-compatible.
    # Receivers that don't know these types silently drop the frame.
    "tool_start",
    "tool_delta",
    "tool_end",
]

InnerAckStage = Literal["received", "processing", "displayed"]
ToolStatus = Literal["running", "done", "failed"]


@dataclass
class InnerMessageFrom:
    role: Literal["app", "plugin"]
    device_id: Optional[str] = None
    device_name: Optional[str] = None
    app_version: Optional[str] = None
    bundle_id: Optional[str] = None


@dataclass
class InnerMessage:
    """The plaintext inside an encrypted relay envelope."""
    t: InnerMessageType
    id: str
    from_: Optional[InnerMessageFrom] = None  # `from` is a Python keyword
    body: dict[str, Any] = field(default_factory=dict)
    ts: int = 0

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {"t": self.t, "id": self.id, "body": self.body, "ts": self.ts}
        if self.from_ is not None:
            wire["from"] = {
                "role": self.from_.role,
                "device_id": self.from_.device_id,
                "device_name": self.from_.device_name,
                "app_version": self.from_.app_version,
                "bundle_id": self.from_.bundle_id,
            }
            # Drop None values to keep the wire shape tight + match TS impl.
            wire["from"] = {k: v for k, v in wire["from"].items() if v is not None}
        return wire


# ─── Consumer-facing outbound enum (what adapter.send() accepts) ───────────


@dataclass
class OutboundText:
    text: str
    kind: Literal["text"] = "text"


@dataclass
class OutboundImage:
    data: bytes
    mime_type: str
    kind: Literal["image"] = "image"


@dataclass
class OutboundAudio:
    data: bytes
    mime_type: str
    duration_ms: int
    waveform: list[float]
    kind: Literal["audio"] = "audio"


@dataclass
class OutboundTextDelta:
    stream_id: str
    delta: str
    kind: Literal["textDelta"] = "textDelta"


@dataclass
class OutboundTextEnd:
    stream_id: str
    text: str
    reset: bool = False
    kind: Literal["textEnd"] = "textEnd"


@dataclass
class OutboundStatus:
    status: Literal["thinking", "typing", "idle"]
    kind: Literal["status"] = "status"


@dataclass
class OutboundAck:
    refs: str
    stage: InnerAckStage
    kind: Literal["ack"] = "ack"


# ─── NEW: Tool-call streaming (Hermes-specific) ────────────────────────────
# Mirrors the text-streaming model: one tool_id per tool invocation. Each
# frame carries a fresh inner.id per §6.4.2; consumers correlate by tool_id.


@dataclass
class OutboundToolStart:
    """Emitted when Hermes begins executing a tool. Args may be truncated
    to keep the wire frame small — Swift app can request full args via a
    follow-up RPC (deferred to v2).

    `icon` is the per-tool emoji from Hermes' agent.display.get_tool_emoji
    registry (skill_view → 📚, todo → 📋, cronjob → ⏰, etc.) — Swift
    app renders it in the bubble header. Empty string = default hammer."""
    tool_id: str          # stable correlator across start/delta/end
    name: str             # e.g. "bash", "read_file", "web.search"
    args: str             # JSON-encoded args, truncated to ~2KB
    icon: str = ""        # tool emoji, e.g. "📚" — empty = use default
    kind: Literal["toolStart"] = "toolStart"


@dataclass
class OutboundToolDelta:
    """Streaming stdout/intermediate output from a long-running tool.
    Optional — fast tools (<200ms) skip directly to tool_end."""
    tool_id: str
    delta: str
    kind: Literal["toolDelta"] = "toolDelta"


@dataclass
class OutboundToolEnd:
    """Emitted on tool completion. `result` is a short summary suitable
    for inline render; the full result lives in the agent's transcript."""
    tool_id: str
    status: ToolStatus
    result: str           # short summary, truncated to ~4KB
    duration_ms: int
    kind: Literal["toolEnd"] = "toolEnd"


OutboundMessage = (
    OutboundText
    | OutboundImage
    | OutboundAudio
    | OutboundTextDelta
    | OutboundTextEnd
    | OutboundStatus
    | OutboundAck
    | OutboundToolStart
    | OutboundToolDelta
    | OutboundToolEnd
)


# ─── Status updates (transport layer → consumer) ───────────────────────────


@dataclass
class StatusUpdate:
    msg_id: str
    status: Literal["sent", "failed"]
    reason: Optional[str] = None


ConnectionState = Literal["disconnected", "connecting", "connected", "reconnecting"]


@dataclass
class ConnectionFailed:
    kind: Literal["failed"]
    reason: str


# ─── Probe / health ────────────────────────────────────────────────────────


@dataclass
class Chat4000Probe:
    ok: bool
    error: Optional[str] = None
    latency_ms: Optional[int] = None
