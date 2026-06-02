"""Hermes plugin-level tool hooks → chat4000.tool events (v2 / Matrix).

Hermes' standard gateway runner fires the cross-cutting `pre_tool_call` /
`post_tool_call` plugin hooks but NOT per-platform reply-pipeline tool callbacks.
So tool bubbles come from here: we filter by `session_id` (chat4000 sessions
only), and push tool events out via the active adapter's `external_tool_start` /
`external_tool_end` (which emit `chat4000.tool` events related to the turn anchor).

Mechanics (same correlation model as v1):
  - Adapters self-register on `__init__` (weakref).
  - `on_session_start` / `pre_llm_call` record which sessions are chat4000.
  - `pre_tool_call` fires before the LLM mints a tool_call_id, so we correlate
    pre↔post with a per-(task_id, tool_name) FIFO queue, not the id.
  - `post_llm_call` sweeps orphans (tools whose post never fired — see
    docs/patches-to-remember.md P1) and closes their bubbles.
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

# (task_id, tool_name) → FIFO of (adapter, tool_id) awaiting their post_tool_call.
_PENDING_TOOL_CALLS: dict[tuple[str, str], list[tuple[Chat4000MatrixAdapter, str]]] = {}

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


def _schedule_async(adapter: Chat4000MatrixAdapter, coro: Coroutine[Any, Any, None]) -> None:
    loop = getattr(adapter, "_loop", None)
    if loop is None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
    if loop is None or not loop.is_running():
        coro.close()
        return
    try:
        loop.create_task(coro)
    except RuntimeError as exc:
        # Loop not running / closed under us — drop the coroutine cleanly.
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
    args: Any = None,  # noqa: ANN401  # dynamic tool-call args from the Hermes host
    task_id: str = "",
    session_id: str = "",
    **_: object,
) -> None:
    adapter = _adapter_for_session(session_id or task_id)
    if adapter is None:
        return
    queue_key = (task_id or session_id, tool_name)

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
        tool_id = await adapter.external_tool_start(tool_name, args or {}, icon)
        if tool_id:
            _PENDING_TOOL_CALLS.setdefault(queue_key, []).append((adapter, tool_id))

    _schedule_async(adapter, _emit())


def on_post_tool_call(
    *,
    tool_name: str = "",
    result: Any = None,  # noqa: ANN401  # dynamic tool-call result from the Hermes host
    task_id: str = "",
    session_id: str = "",
    **_: object,
) -> None:
    queue_key = (task_id or session_id, tool_name)
    queue = _PENDING_TOOL_CALLS.get(queue_key)
    if not queue:
        return
    adapter, tool_id = queue.pop(0)
    if not queue:
        _PENDING_TOOL_CALLS.pop(queue_key, None)

    result_text = result if isinstance(result, str) else ("" if result is None else str(result))
    status = "failed" if result_text.startswith(("[error", "Error", "Traceback")) else "done"

    async def _emit() -> None:
        await adapter.external_tool_end(tool_id, status=status, result=result_text)

    _schedule_async(adapter, _emit())


def on_post_llm_call(*, session_id: str = "", platform: str = "", **_: object) -> None:
    """End-of-turn orphan sweep (P1). Some tools (todo/memory/…) are intercepted
    by the agent loop and never fire post_tool_call → their bubble would spin
    forever. Close them with a synthetic done."""
    if not session_id or (platform or "").strip().lower() != "chat4000":
        return
    orphans = [
        (key, queue)
        for key, queue in list(_PENDING_TOOL_CALLS.items())
        if key[0] == session_id and queue
    ]
    for key, queue in orphans:
        while queue:
            adapter, tool_id = queue.pop(0)

            async def _close(a: Chat4000MatrixAdapter = adapter, t: str = tool_id) -> None:
                await a.external_tool_end(t, status="done", result="")

            _schedule_async(adapter, _close())
        _PENDING_TOOL_CALLS.pop(key, None)


def register_plugin_hooks(ctx: Any) -> None:  # noqa: ANN401  # Hermes host plugin context (untyped host object)
    try:
        ctx.register_hook("on_session_start", on_session_start)
        ctx.register_hook("on_session_end", on_session_end)
        ctx.register_hook("pre_llm_call", on_pre_llm_call)
        ctx.register_hook("post_llm_call", on_post_llm_call)
        ctx.register_hook("pre_tool_call", on_pre_tool_call)
        ctx.register_hook("post_tool_call", on_post_tool_call)
        logger.info("chat4000: v2 tool-call hooks registered")
    except Exception as exc:  # noqa: BLE001
        # Hook registration is optional (tool bubbles); a host that lacks
        # register_hook must not crash the plugin. Report once, then continue.
        from .error_log import dump_chat4000_trace

        logger.warning("chat4000: hook registration failed: %s", exc)
        dump_chat4000_trace("plugin_hooks.register", exc)
