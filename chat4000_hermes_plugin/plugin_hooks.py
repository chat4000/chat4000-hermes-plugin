"""Hermes plugin-level tool hooks → chat4000.tool events (v2 / Matrix).

Tool calls are START-ONLY (protocol E): ONE `chat4000.tool` event per tool, sent
when it starts, never updated. There is no END, no `m.replace` edit, no
result/status/duration, and therefore no pre↔post correlation, no FIFO queue, and
no orphan-flush — we only hook `pre_tool_call`.

Mechanics:
  - Adapters self-register on `__init__` (weakref).
  - `on_session_start` / `pre_llm_call` record which sessions are chat4000.
  - `pre_tool_call` reads the firing turn's room from Hermes' task-local chat
    contextvar (synchronously, on the executor thread — correct under concurrency)
    and emits one START event via the active adapter's `external_tool_start`.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .matrix.hermes_adapter import Chat4000MatrixAdapter

logger = logging.getLogger(__name__)

_ACTIVE_ADAPTERS: weakref.WeakSet[Chat4000MatrixAdapter] = weakref.WeakSet()

# session_id → "chat4000" (populated by on_session_start / pre_llm_call).
_SESSION_PLATFORM: dict[str, str] = {}


def register_active_adapter(adapter: Chat4000MatrixAdapter) -> None:
    _ACTIVE_ADAPTERS.add(adapter)


def deregister_active_adapter(adapter: Chat4000MatrixAdapter) -> None:
    _ACTIVE_ADAPTERS.discard(adapter)


def _adapter_for_session(session_id: str) -> Chat4000MatrixAdapter | None:
    if not session_id or _SESSION_PLATFORM.get(session_id) != "chat4000":
        return None
    for adapter in list(_ACTIVE_ADAPTERS):
        if getattr(adapter, "_connected", False):
            return adapter
    return None


def _current_chat_id() -> str:
    """The current turn's chat_id (== room_id for Matrix), read from Hermes'
    task-local session ContextVar. This is how concurrent turns stay isolated:
    the var is per-task and Hermes preserves it across the run_in_executor hop
    where these hooks fire.

    MUST be called SYNCHRONOUSLY in the sync hook body (the executor thread, where
    the var is live) — NEVER inside the coroutine we hand to the gateway loop via
    run_coroutine_threadsafe, where it runs on a different thread/context and the
    var is unset."""
    try:
        from gateway.session_context import get_session_env

        return get_session_env("HERMES_SESSION_CHAT_ID", "") or ""
    except Exception:  # noqa: BLE001  # host var absent (tests / pre-turn) → no room
        return ""


def _schedule_async(adapter: Chat4000MatrixAdapter, coro: Coroutine[Any, Any, None]) -> None:
    """Run `coro` on the adapter's gateway event loop.

    CRITICAL: the plugin tool hooks (pre/post_tool_call) fire SYNCHRONOUSLY on
    Hermes' agent worker thread — the gateway runs the agent + tool dispatch via
    `loop.run_in_executor(...)`, so the hook is NOT on the gateway event-loop
    thread. `loop.create_task` is loop-thread-only and raises cross-thread; we
    must hand the coroutine to the loop with `run_coroutine_threadsafe`, or every
    chat4000.tool emit is silently dropped (the historical bug — tools stopped
    reaching the client while in-loop reply-pipeline writes kept working)."""
    loop = getattr(adapter, "_loop", None)
    if loop is None or not loop.is_running():
        coro.close()
        return
    try:
        on_loop_thread = asyncio.get_running_loop() is loop
    except RuntimeError:
        on_loop_thread = False  # no loop in this thread → we're on a worker thread
    try:
        if on_loop_thread:
            loop.create_task(coro)
        else:
            asyncio.run_coroutine_threadsafe(coro, loop)  # worker thread → hand off
    except RuntimeError as exc:
        # Loop closed under us — drop the coroutine cleanly.
        logger.debug("chat4000 hook schedule failed: %s", exc)
        coro.close()


# ─── session classification ───────────────────────────────────────────────


def on_session_start(*, session_id: str = "", platform: str = "", **_: object) -> None:
    if session_id and (platform or "").strip().lower() == "chat4000":
        _SESSION_PLATFORM[session_id] = "chat4000"


def on_pre_llm_call(*, session_id: str = "", platform: str = "", **_: object) -> None:
    if session_id and (platform or "").strip().lower() == "chat4000":
        _SESSION_PLATFORM.setdefault(session_id, "chat4000")


def on_session_end(*, session_id: str = "", **_: object) -> None:
    if session_id:
        _SESSION_PLATFORM.pop(session_id, None)


# ─── tool lifecycle → chat4000.tool events ─────────────────────────────────


def on_pre_tool_call(
    *,
    tool_name: str = "",
    args: Any = None,  # noqa: ANN401  # accepted for hook compat; NOT sent (START-only)
    task_id: str = "",
    session_id: str = "",
    **_: object,
) -> None:
    """START-ONLY (protocol E): emit ONE chat4000.tool event when a tool starts.
    There is no post_tool_call END and no round-boundary flush — the event is
    fire-and-forget, so no correlation/queue is needed."""
    adapter = _adapter_for_session(session_id or task_id)
    if adapter is None:
        return
    # Read the firing turn's room HERE, synchronously, on the executor thread where
    # Hermes' task-local contextvar is live — this is what keeps concurrent turns
    # from bleeding. We thread it into the (later, other-thread) coroutine below.
    room = _current_chat_id()

    icon = ""
    try:
        from agent.display import get_tool_emoji  # type: ignore[import-not-found]

        icon = get_tool_emoji(tool_name, default="")
    except Exception as exc:  # noqa: BLE001
        # The emoji registry is a cosmetic, optional Hermes-host import; report
        # once and fall back to no icon.
        from .error_log import dump_chat4000_trace

        dump_chat4000_trace("plugin_hooks.tool_emoji", exc)

    async def _emit() -> None:
        await adapter.external_tool_start(
            tool_name, args, icon, room=room, session_id=session_id or task_id
        )

    _schedule_async(adapter, _emit())


def register_plugin_hooks(ctx: Any) -> None:  # noqa: ANN401  # Hermes host plugin context (untyped host object)
    try:
        ctx.register_hook("on_session_start", on_session_start)
        ctx.register_hook("on_session_end", on_session_end)
        ctx.register_hook("pre_llm_call", on_pre_llm_call)
        ctx.register_hook("pre_tool_call", on_pre_tool_call)
        logger.info("chat4000: v2 tool-call hooks registered")
    except Exception as exc:  # noqa: BLE001
        # Hook registration is optional (tool bubbles); a host that lacks
        # register_hook must not crash the plugin. Report once, then continue.
        from .error_log import dump_chat4000_trace

        logger.warning("chat4000: hook registration failed: %s", exc)
        dump_chat4000_trace("plugin_hooks.register", exc)
