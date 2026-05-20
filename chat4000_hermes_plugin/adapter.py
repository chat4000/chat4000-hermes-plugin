"""Chat4000 platform adapter for Hermes — the entry point Hermes calls
via plugin.yaml + the platform registry.

This is the analog of clawconnect-plugin/src/channel.ts but rewritten
against Hermes' BasePlatformAdapter contract instead of OpenClaw's
defineBundledChannelEntry. Same protocol, same crypto, same relay —
just a different host SDK.

Responsibilities:
  - Implement BasePlatformAdapter (connect/disconnect/send/get_chat_info)
  - On inbound message: decrypt → dispatch to Hermes' agent runner
  - On outbound (agent → user): forward via StreamDispatcher
  - On tool-call lifecycle: forward via ToolCallDispatcher (NEW)
  - Emit Flow B inner ack on app-origin text/image/audio per §6.6.5

Hermes integration points:
  - register(ctx): registered platform name "chat4000", emoji 🔐,
    label "chat4000"
  - hooks `on_tool_start` / `on_tool_output` / `on_tool_end` from the
    agent reply pipeline (provided by Hermes core, exposed via
    `replyOptions` in the reply pipeline construction)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from typing import Any, Optional

from .accounts import resolve_chat4000_account
from .dispatch.stream_dispatcher import StreamDispatcher
from .dispatch.tool_call_dispatcher import ToolCallDispatcher
from .session_binding import (
    get_chat4000_session_binding,
    pick_default_hermes_session,
)
from .transport import GroupConfig as TransportGroupConfig
from .transport.registry import (
    get_transport,
    register_transport,
    unregister_transport,
)
from .transport.relay import RelayMessageTransport
from .protocol_types import (
    ConnectionFailed,
    InnerMessage,
    OutboundAck,
    OutboundAudio,
    OutboundImage,
    OutboundStatus,
    OutboundText,
)

logger = logging.getLogger(__name__)

# Lazy imports below — Hermes' BasePlatformAdapter lives in the host
# process. We avoid importing at module top so test/CI runs without the
# Hermes core present still pass.


class Chat4000Adapter:  # subclass of BasePlatformAdapter, lazily resolved
    """The actual class declaration uses BasePlatformAdapter as base.
    We monkey-patch the bases at register() time so this module is
    importable without Hermes present (e.g. unit tests for crypto).

    The Hermes SDK contract:
      connect() -> bool        — async, return True on success
      disconnect()             — async, clean shutdown
      send(chat_id, content, *, reply_to=None, metadata=None) -> SendResult
      send_typing(chat_id)
      send_image(chat_id, image_url, caption) -> SendResult
      get_chat_info(chat_id) -> dict
    """

    def __init__(self, config, **kwargs):
        # Resolve Hermes types lazily so this module imports without Hermes.
        from gateway.platforms.base import BasePlatformAdapter  # type: ignore[import-not-found]
        from gateway.config import Platform  # type: ignore[import-not-found]

        # Hand-wired super call (the type-system-level inheritance is set
        # at register() time via _make_adapter_class).
        BasePlatformAdapter.__init__(
            self, config=config, platform=Platform("chat4000")
        )

        extra = getattr(config, "extra", {}) or {}
        self._account_id = extra.get("accountId") or extra.get("account_id") or "default"
        self._config = config
        self._cfg = extra  # raw extras for resolve_chat4000_account
        self._transport: Optional[RelayMessageTransport] = None
        self._abort_signal = asyncio.Event()
        self._stream_dispatcher: Optional[StreamDispatcher] = None
        self._tool_dispatcher: Optional[ToolCallDispatcher] = None
        self._connected = False
        self._handlers_unsubscribe: list = []
        # Captured at connect-time for plugin_hooks to schedule async
        # frame emissions on the right asyncio loop.
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # Make this adapter visible to plugin-level tool hooks. Weakref-
        # backed, so a crashed adapter doesn't leak.
        from .plugin_hooks import register_active_adapter
        register_active_adapter(self)

    @property
    def name(self) -> str:
        return "chat4000"

    # ─── BasePlatformAdapter — lifecycle ─────────────────────────────────

    async def connect(self) -> bool:
        # Capture the running event loop so plugin_hooks (called from the
        # synchronous tool-execution path) can schedule async frame
        # emissions back onto our loop.
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

        # Resolve the account from extras (config.yaml) merged with env.
        # We pass a synthetic `cfg` shape that resolve_chat4000_account
        # understands so we can reuse the TS-port logic 1:1.
        synthetic_cfg = {"channels": {"chat4000": {"accounts": {self._account_id: self._cfg}}}}
        account = resolve_chat4000_account(synthetic_cfg, self._account_id)

        if not account.configured:
            logger.error(
                "chat4000 not configured for account %r — run `hermes chat4000 pair`",
                self._account_id,
            )
            return False

        transport = RelayMessageTransport(abort_signal=self._abort_signal)
        register_transport(self._account_id, transport)
        self._transport = transport

        # Inbound dispatch — decrypt, then route to Hermes agent or to
        # inner-side handlers (acks, streaming chunks from peer apps).
        unsub_recv = transport.on_receive(self._on_inner_received)
        unsub_state = transport.on_connection_state(self._on_connection_state)
        self._handlers_unsubscribe = [unsub_recv, unsub_state]

        transport.connect(
            TransportGroupConfig(
                account_id=account.account_id,
                group_id=account.group_id,
                group_key_bytes=account.group_key_bytes,
                relay_url=account.relay_url,
                release_channel=account.config.release_channel,
                runtime_log_level=account.runtime_log_level,
            )
        )

        # Build the tool-call dispatcher at connect-time so plugin_hooks
        # can push frames as soon as Hermes' tool_executor invokes our
        # pre_tool_call / post_tool_call hooks. Previously this lived
        # inside reply_pipeline_options() — which Hermes never calls on
        # the standard run path — so the dispatcher was permanently None
        # and every tool frame got dropped.
        self._tool_dispatcher = ToolCallDispatcher(
            send=lambda msg: self._transport.send(msg) if self._transport else None,  # type: ignore[union-attr]
        )

        self._mark_connected()  # BasePlatformAdapter helper
        self._connected = True
        return True

    async def disconnect(self) -> None:
        self._connected = False
        from .plugin_hooks import deregister_active_adapter
        deregister_active_adapter(self)
        self._abort_signal.set()
        for unsub in self._handlers_unsubscribe:
            try:
                unsub()
            except Exception:
                pass
        self._handlers_unsubscribe.clear()
        if self._stream_dispatcher is not None:
            self._stream_dispatcher.dispose()
            self._stream_dispatcher = None
        if self._tool_dispatcher is not None:
            self._tool_dispatcher.dispose()
            self._tool_dispatcher = None
        if self._transport is not None:
            await self._transport.disconnect()
            unregister_transport(self._account_id)
            self._transport = None
        try:
            self._mark_disconnected()
        except Exception:
            pass

    # ─── BasePlatformAdapter — sending ───────────────────────────────────

    async def send(
        self,
        chat_id: str,
        content,
        *,
        reply_to: Optional[str] = None,
        metadata: Optional[dict] = None,
    ):
        """Hermes calls this when the agent has a final reply to deliver.

        For text replies that came through the agent's streaming pipeline,
        we've already been forwarding text_delta / text_end via the
        per-turn StreamDispatcher and `content` here is the assembled
        text we can ignore (the dispatcher already closed the stream).

        For oneshot text replies (non-streaming agents, slash-command
        responses, error messages), we send a plain `text` frame."""
        from gateway.platforms.base import SendResult  # type: ignore[import-not-found]

        if self._transport is None:
            return SendResult(success=False, error="transport not connected")

        # `content` shape per BasePlatformAdapter: usually a string;
        # sometimes a dict with `text` + `media_url`. Be defensive.
        if isinstance(content, dict):
            text = content.get("text", "") or ""
            media_url = content.get("media_url")
        else:
            text = str(content or "")
            media_url = None

        if media_url:
            # V1: surface the media URL as an inline link inside text. The
            # native media-attachment path (file/image bytes over the wire)
            # is Tier 2-F and not in scope.
            text = (text + ("\n\n" if text else "") + f"Attachment: {media_url}").strip()

        if not text:
            return SendResult(success=True, message_id="")

        # If the StreamDispatcher already closed this turn's stream, send
        # an oneshot text frame. Otherwise it means we never streamed and
        # this is a complete reply.
        wire_id = self._transport.send(OutboundText(text=text))
        return SendResult(success=True, message_id=wire_id)

    async def send_typing(self, chat_id: str) -> None:
        if self._transport is None:
            return
        self._transport.send(OutboundStatus(status="typing"))

    async def send_image(self, chat_id, image_url, caption=None):
        from gateway.platforms.base import SendResult  # type: ignore[import-not-found]
        # V1: outgoing image via URL — surface as link in a text frame.
        # Native image transport from plugin to app is symmetric to inbound
        # but not commonly used by Hermes agents (most images come from
        # the user toward the agent, not the other way).
        text = (caption + "\n\n" if caption else "") + f"Image: {image_url}"
        if self._transport is None:
            return SendResult(success=False, error="transport not connected")
        wire_id = self._transport.send(OutboundText(text=text))
        return SendResult(success=True, message_id=wire_id)

    async def get_chat_info(self, chat_id) -> dict:
        return {"name": f"chat4000 ({chat_id[:8]}...)", "type": "dm", "chat_id": chat_id}

    # ─── Hermes reply-pipeline hooks (streaming + tool calls) ────────────

    def reply_pipeline_options(self) -> dict:
        """Returned to Hermes' reply-pipeline factory so the agent's
        per-turn streaming + tool-call events flow into our dispatchers.

        Hermes' reply pipeline currently exposes:
          - on_reasoning_stream / on_reasoning_end
          - on_assistant_message_start
          - on_partial_reply(payload: { text })
          - on_tool_start(name, args)
          - on_tool_output(tool_id, delta)         [may not always fire]
          - on_tool_end(tool_id, status, result)

        We map these into chat4000 wire frames."""
        if self._transport is None:
            return {}

        # Fresh dispatchers per turn.
        self._stream_dispatcher = StreamDispatcher(
            send=lambda msg: self._transport.send(msg) if self._transport else None,  # type: ignore[union-attr]
        )
        self._tool_dispatcher = ToolCallDispatcher(
            send=lambda msg: self._transport.send(msg) if self._transport else None,  # type: ignore[union-attr]
        )

        # Hermes' agent pipeline doesn't natively expose a per-call tool_id
        # the way our dispatcher wants — we mint one on tool_start and
        # thread it through. Maintain a per-turn name→tool_id map for the
        # cases where on_tool_output/on_tool_end only carry the tool name.
        active_tool_ids_by_name: dict[str, list[str]] = {}

        async def on_reasoning_stream(_payload: dict) -> None:
            self._transport.send(OutboundStatus(status="thinking"))  # type: ignore[union-attr]

        async def on_reasoning_end(_payload: dict) -> None:
            self._transport.send(OutboundStatus(status="typing"))  # type: ignore[union-attr]

        async def on_assistant_message_start(_payload: dict) -> None:
            self._transport.send(OutboundStatus(status="typing"))  # type: ignore[union-attr]

        async def on_partial_reply(payload: dict) -> None:
            text = payload.get("text") or ""
            if not text:
                return
            self._transport.send(OutboundStatus(status="typing"))  # type: ignore[union-attr]
            if self._stream_dispatcher is not None:
                await self._stream_dispatcher.on_partial(text)

        async def on_tool_start(name: str, args) -> str:
            if self._tool_dispatcher is None:
                return ""
            self._transport.send(OutboundStatus(status="thinking"))  # type: ignore[union-attr]
            tool_id = await self._tool_dispatcher.on_tool_start(name=name, args=args)
            active_tool_ids_by_name.setdefault(name, []).append(tool_id)
            return tool_id

        async def on_tool_output(tool_id_or_name: str, delta: str) -> None:
            if self._tool_dispatcher is None:
                return
            tool_id = _resolve_tool_id(tool_id_or_name, active_tool_ids_by_name)
            if tool_id is None:
                return
            await self._tool_dispatcher.on_tool_output(tool_id, delta)

        async def on_tool_end(tool_id_or_name: str, *, status: str = "done", result: str = "") -> None:
            if self._tool_dispatcher is None:
                return
            tool_id = _resolve_tool_id(tool_id_or_name, active_tool_ids_by_name, pop=True)
            if tool_id is None:
                return
            await self._tool_dispatcher.on_tool_end(
                tool_id, status=status, result=result  # type: ignore[arg-type]
            )

        async def on_final(payload: dict) -> None:
            text = payload.get("text") or ""
            if self._stream_dispatcher is None:
                return
            outcome = await self._stream_dispatcher.on_final(text)
            if outcome == "oneshot" and text:
                self._transport.send(OutboundText(text=text))  # type: ignore[union-attr]
            self._transport.send(OutboundStatus(status="idle"))  # type: ignore[union-attr]

        return {
            "on_reasoning_stream": on_reasoning_stream,
            "on_reasoning_end": on_reasoning_end,
            "on_assistant_message_start": on_assistant_message_start,
            "on_partial_reply": on_partial_reply,
            "on_tool_start": on_tool_start,
            "on_tool_output": on_tool_output,
            "on_tool_end": on_tool_end,
            "on_final": on_final,
        }

    # ─── Inbound dispatch ────────────────────────────────────────────────

    def _on_inner_received(self, inner: InnerMessage) -> Any:
        """Called once per decrypted+dedup'd inbound inner message.

        We don't aggregate inbound streamed text (text_delta/text_end from
        peer apps) — Hermes agents talk to ONE chat4000 group at a time
        and the only streamed sender we care about is the agent itself
        going the OTHER direction. Same logic as the TS plugin's
        `handleInbound` for the "ignore inbound stream" branch."""
        is_from_app = inner.from_ is not None and inner.from_.role == "app"

        if inner.t == "ack":
            # Plugin-side acks not used in v1.
            return

        if inner.t in ("text_delta", "text_end", "status", "tool_start", "tool_delta", "tool_end"):
            # Anything we don't dispatch into the agent runner.
            return

        if inner.t not in ("text", "image", "audio"):
            return

        # Emit Flow B inner ack BEFORE running the agent so the iPhone
        # ✓✓ tick lights up immediately, not after token generation.
        if is_from_app and self._transport is not None:
            try:
                self._transport.send(OutboundAck(refs=inner.id, stage="received"))
            except Exception:
                pass

        # Dispatch to the Hermes agent runner via BasePlatformAdapter.
        return asyncio.ensure_future(self._dispatch_to_agent(inner))

    async def _dispatch_to_agent(self, inner: InnerMessage) -> None:
        """Hand the inbound text/image/audio to Hermes.

        BasePlatformAdapter.handle_message is the canonical entry point —
        it constructs a MessageEvent, builds the SessionSource, and routes
        through the gateway's session-resolution + agent-dispatch pipeline.
        That's the exact path the Telegram/Slack/Discord adapters take."""
        from gateway.platforms.base import MessageEvent, MessageType  # type: ignore[import-not-found]

        # Map inner.body → MessageEvent payload.
        if inner.t == "text":
            text = (inner.body or {}).get("text", "")
            message_type = MessageType.TEXT
            payload = {"text": text}
        elif inner.t == "image":
            message_type = MessageType.IMAGE
            payload = {
                "data_base64": (inner.body or {}).get("data_base64", ""),
                "mime_type": (inner.body or {}).get("mime_type", "image/jpeg"),
            }
        elif inner.t == "audio":
            message_type = MessageType.AUDIO
            payload = {
                "data_base64": (inner.body or {}).get("data_base64", ""),
                "mime_type": (inner.body or {}).get("mime_type", "audio/mp4"),
                "duration_ms": (inner.body or {}).get("duration_ms", 0),
                "waveform": (inner.body or {}).get("waveform") or [],
            }
        else:
            return

        # Build the SessionSource via BasePlatformAdapter helper so the
        # gateway recognises us as a regular platform.
        source = self.build_source(
            chat_id=self._account_id,
            user_id=(inner.from_.device_id if inner.from_ else None) or self._account_id,
            chat_type="dm",
        )

        event = MessageEvent(
            text=payload.get("text", ""),
            message_type=message_type,
            source=source,
            raw_message=inner.to_wire(),
            message_id=inner.id,
        )

        # handle_message is BasePlatformAdapter's bridge into the gateway
        # runner. The runner then constructs the agent, sets up the reply
        # pipeline with our `reply_pipeline_options`, and ships a final
        # `deliver` back into self.send(...).
        await self.handle_message(event)

    def _on_connection_state(self, state: Any) -> None:
        if isinstance(state, dict) and state.get("kind") == "failed":
            logger.warning("chat4000 relay failed: %s", state.get("reason"))
        elif state == "connected":
            logger.info("chat4000 connected to relay")
        elif state in ("disconnected", "reconnecting"):
            logger.info("chat4000 relay state: %s", state)


# ─── Hermes entry points ──────────────────────────────────────────────────


def _resolve_tool_id(
    candidate: str,
    map_by_name: dict[str, list[str]],
    *,
    pop: bool = False,
) -> Optional[str]:
    """Hermes' tool callbacks may pass either the tool_id we returned from
    on_tool_start, OR the tool name (depending on which hook point fired).
    Try the candidate as an id first; fall back to the FIFO of active
    tool_ids for that name."""
    if any(candidate == tid for tids in map_by_name.values() for tid in tids):
        # It IS a tool_id; pop from whichever name's list owns it.
        if pop:
            for tids in map_by_name.values():
                if candidate in tids:
                    tids.remove(candidate)
                    break
        return candidate
    tids = map_by_name.get(candidate)
    if not tids:
        return None
    return tids.pop(0) if pop else tids[0]


def check_requirements() -> bool:
    """Hermes calls this at adapter discovery to gate loading. The plugin
    is always loadable — the user just won't get a working group until
    they run `hermes chat4000 pair`."""
    return True


def validate_config(config) -> bool:
    """Whether the platform is configured well enough to connect. We accept
    either an env var override or a key file on disk. Both are checked by
    resolve_chat4000_account()."""
    # CHAT4000_GROUP_KEY env or a stored key file under ~/.hermes/plugins/chat4000/
    if os.getenv("CHAT4000_GROUP_KEY", "").strip():
        return True
    from .accounts import resolve_chat4000_account

    account = resolve_chat4000_account(None, None)
    return account.configured


def _env_enablement() -> Optional[dict]:
    """Auto-enable the platform when CHAT4000_GROUP_KEY is set or a key
    file exists. Hermes calls this BEFORE adapter construction so
    `hermes gateway status` sees the right state."""
    from .accounts import resolve_chat4000_account

    account = resolve_chat4000_account(None, None)
    if not account.configured:
        return None
    return {
        "accountId": account.account_id,
        "groupId": account.group_id,
        "home_channel": {"chat_id": account.group_id, "name": "chat4000"},
    }


def _make_adapter_class():
    """Hermes' BasePlatformAdapter is only importable from inside the
    Hermes process. Build the real class dynamically so the module
    imports cleanly during unit tests / CI."""
    from gateway.platforms.base import BasePlatformAdapter  # type: ignore[import-not-found]

    # Preserve everything except a few class-machinery dunders Python
    # populates automatically. In particular, keep `__init__` — without
    # it the dynamic class inherits BasePlatformAdapter.__init__, which
    # needs a `platform` positional arg the factory doesn't pass.
    _SKIP = {"__dict__", "__weakref__", "__module__", "__qualname__"}
    namespace = {
        k: v for k, v in Chat4000Adapter.__dict__.items() if k not in _SKIP
    }
    return type(
        "Chat4000Adapter",
        (BasePlatformAdapter,),
        namespace,
    )


def register(ctx) -> None:
    """Plugin entry point — Hermes' plugin loader calls this once on
    discovery. We register our platform via ctx.register_platform.

    The registry call also wires CLI subcommands (`hermes chat4000 ...`)
    — those live in src/cli.py and use the same ctx.register_cli surface
    Hermes' built-in plugins use."""
    from .plugin_hooks import register_plugin_hooks
    from .telemetry import initialize_chat4000_telemetry

    initialize_chat4000_telemetry()

    # Wire Hermes' cross-cutting tool-call hooks so the iOS app sees
    # tool_start / tool_end bubbles for every tool the agent invokes
    # in chat4000 sessions. The hooks self-filter by session_id.
    register_plugin_hooks(ctx)

    # chat4000's auth IS the 32-byte group key: anyone with it can already
    # decrypt every message. Hermes' per-user pairing on top would just
    # mean "pair the device once with chat4000 E2E, then ALSO ask the bot
    # owner to approve a code". Skip the second layer by default — users
    # who want platform-level allowlists can set CHAT4000_ALLOW_ALL_USERS
    # to "false" and use CHAT4000_ALLOWED_USERS like other platforms.
    if "CHAT4000_ALLOW_ALL_USERS" not in os.environ:
        os.environ["CHAT4000_ALLOW_ALL_USERS"] = "true"

    AdapterClass = _make_adapter_class()
    # Chat4000Adapter.__init__ does the BasePlatformAdapter super-call
    # itself (with `platform=Platform("chat4000")`) — factory only passes
    # the PlatformConfig.
    ctx.register_platform(
        name="chat4000",
        label="chat4000",
        adapter_factory=lambda cfg: AdapterClass(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        env_enablement_fn=_env_enablement,
        required_env=[],
        install_hint="Run `chat4000 pair` to pair a device.",
        max_message_length=4096,
        # Wire Hermes' auth allowlist envs to chat4000-specific names so
        # the gateway's _is_user_authorized branch resolves them via the
        # plugin registry instead of falling through to GATEWAY_*.
        allowed_users_env="CHAT4000_ALLOWED_USERS",
        allow_all_env="CHAT4000_ALLOW_ALL_USERS",
        platform_hint=(
            "You are chatting via chat4000 (encrypted iOS/macOS/CLI client). "
            "It supports markdown formatting and streams replies as text_delta "
            "frames. Tool calls render natively in the chat as expandable "
            "bubbles — keep tool args readable."
        ),
        emoji="🔐",
    )

    # CLI subcommands (hermes chat4000 pair / setup / ...) live in .cli
    # and are gated behind ctx.register_cli, which isn't present on
    # every Hermes version. Skip registration when the surface is
    # missing rather than crashing the whole plugin load.
    if hasattr(ctx, "register_cli"):
        from .cli import register_chat4000_cli
        register_chat4000_cli(ctx)
