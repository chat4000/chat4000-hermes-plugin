"""Hermes plugin-level hooks → chat4000 tool_start / tool_end wire frames.

Hermes' standard gateway runner doesn't wire per-platform tool callbacks
(only `gateway/platforms/api_server.py` does, via `tool_start_callback`
and `tool_complete_callback`). To get tool-call bubbles in the chat4000
iOS/macOS app we register Hermes' cross-cutting `pre_tool_call` and
`post_tool_call` plugin hooks, filter by `session_id` (which carries the
platform name in its key), and push frames out via the active
Chat4000Adapter's `_tool_dispatcher`.

Mechanics:
  - Adapters self-register on `__init__` (weakref so we don't pin them).
  - The hooks fire synchronously from inside `agent.tool_executor`, which
    is itself running inside the gateway's asyncio loop. We schedule the
    actual async frame emission via `loop.create_task` from the same
    loop the adapter is using — no thread-bridge needed.
  - `tool_call_id` from Hermes is used as the correlator between
    `pre_tool_call` and `post_tool_call`. We cache the (adapter, our
    minted tool_id) pair under that key so the matching `post_tool_call`
    reaches the same adapter even if the active-set has changed.

Reasoning (`agent.reasoning_callback`) lives at the agent-init level
and is NOT exposed as a plugin hook in Hermes v0.14.0. Surfacing
reasoning needs a separate path (monkeypatch agent_init, or override
`handle_message` to construct the agent ourselves). Out of scope for
this F1 work — see follow-up TODO in the plugin README.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Live Chat4000Adapter instances. WeakSet so a disconnected adapter that
# was never explicitly deregistered (crash, GC race) doesn't leak.
_ACTIVE_ADAPTERS: "weakref.WeakSet" = weakref.WeakSet()

# Pre/post correlation queue, keyed by (task_id, tool_name).
#
# Hermes' hook surface is asymmetric: `pre_tool_call` fires BEFORE the
# tool_call_id is minted by the LLM provider (so the kwarg arrives
# empty), but `post_tool_call` fires AFTER and DOES carry the id. We
# can't bridge by tool_call_id. Hermes' own langfuse plugin solves the
# same problem with a per-(task_id, tool_name) queue — pre appends, post
# pops in FIFO order. Lists not dicts so concurrent tool_calls of the
# same name (parallel web.search) don't clobber each other.
_PENDING_TOOL_CALLS: dict[tuple[str, str], list[tuple[Any, str]]] = {}

# session_id → platform name. Populated by the `on_session_start` hook.
# Hermes passes the AGENT's session_id (timestamp+hash) to tool hooks —
# NOT the session-key format `agent:main:{platform}:...` — so parsing the
# id to derive platform doesn't work. on_session_start is the only hook
# that ships the platform kwarg alongside the session_id. We record the
# mapping there and consult it in the tool hooks.
_SESSION_PLATFORM: dict[str, str] = {}


def register_active_adapter(adapter) -> None:
    _ACTIVE_ADAPTERS.add(adapter)


def deregister_active_adapter(adapter) -> None:
    _ACTIVE_ADAPTERS.discard(adapter)


def _adapter_for_session(session_id: str):
    """Return the active chat4000 adapter for a Hermes agent session_id.

    Routing is via the `_SESSION_PLATFORM` map populated by the
    `on_session_start` hook — the agent's session_id alone (a timestamp
    hash) doesn't encode the platform, so we have to remember the
    mapping. Sessions created BEFORE this plugin loaded are unroutable
    by design; users must `/new` (or reset the session) once after
    install, then routing works for the lifetime of every new session."""
    if not session_id:
        return None
    if _SESSION_PLATFORM.get(session_id) != "chat4000":
        return None
    for adapter in list(_ACTIVE_ADAPTERS):
        if getattr(adapter, "_connected", False):
            return adapter
    return None


def on_session_start(
    *,
    session_id: str = "",
    platform: str = "",
    **_: Any,
) -> None:
    """Record the platform for this agent session_id so subsequent
    `pre_tool_call` / `post_tool_call` hooks can route correctly.

    Only chat4000 sessions are kept; other platforms are dropped to
    avoid unbounded growth (sessions in long-lived gateways)."""
    if not session_id:
        return
    plat = (platform or "").strip().lower()
    if plat == "chat4000":
        _SESSION_PLATFORM[session_id] = "chat4000"


def on_pre_llm_call(
    *,
    session_id: str = "",
    platform: str = "",
    **_: Any,
) -> None:
    """Fires once per LLM turn (every conversation step). Carries both
    `session_id` and `platform`. We use it to populate
    `_SESSION_PLATFORM` lazily — `on_session_start` only fires for
    BRAND-NEW sessions, so sessions that pre-date the plugin install
    would otherwise never be classified. pre_llm_call fills the gap on
    the first turn after install."""
    logger.info(
        "chat4000.pre_llm_call: session=%s platform=%r",
        session_id, platform,
    )
    if not session_id:
        return
    plat = (platform or "").strip().lower()
    if plat == "chat4000":
        if _SESSION_PLATFORM.get(session_id) != "chat4000":
            _SESSION_PLATFORM[session_id] = "chat4000"
            logger.info("chat4000: recorded session %s for routing", session_id)


def on_post_llm_call(
    *,
    session_id: str = "",
    platform: str = "",
    **_: Any,
) -> None:
    """End-of-turn sweep for orphan tool bubbles.

    PATCH — see docs/patches-to-remember.md §P1.

    Hermes' pre/post_tool_call hooks are asymmetric. `_AGENT_LOOP_TOOLS`
    (todo, memory, session_search, delegate_task) are intercepted by
    agent/tool_executor.py before reaching model_tools.handle_function_call,
    so `pre_tool_call` fires but `post_tool_call` never does. The plugin's
    correlation queue keeps the entry forever; iOS spinner spins forever.

    post_llm_call fires at end of turn (conversation_loop.py:3965) and
    carries session_id + platform. For chat4000 sessions, drain queued
    orphans and emit synthetic tool_end frames so the iOS bubbles close.

    Removed when Hermes fixes the asymmetry."""
    if not session_id:
        return
    plat = (platform or "").strip().lower()
    if plat != "chat4000":
        return

    orphans = [
        (key, queue) for key, queue in list(_PENDING_TOOL_CALLS.items())
        if key[0] == session_id and queue
    ]
    if not orphans:
        return
    logger.info(
        "chat4000.post_llm_call: sweeping %d orphan tool bubble(s) for %s",
        sum(len(q) for _, q in orphans), session_id,
    )
    for key, queue in orphans:
        while queue:
            adapter, our_id = queue.pop(0)
            if adapter is None or adapter._tool_dispatcher is None:
                continue

            async def _emit_close(a=adapter, tid=our_id) -> None:
                await a._tool_dispatcher.on_tool_end(
                    tid, status="done", result=""
                )

            _schedule_async(adapter, _emit_close())
        _PENDING_TOOL_CALLS.pop(key, None)


def on_session_end(
    *,
    session_id: str = "",
    **_: Any,
) -> None:
    """Free the routing entry when a session ends."""
    if session_id:
        _SESSION_PLATFORM.pop(session_id, None)


def _schedule_async(adapter, coro) -> None:
    """Schedule the async emission on the adapter's asyncio loop.

    Hooks fire from inside `agent.tool_executor`, which is itself running
    on the gateway's event loop. `get_running_loop()` returns that loop
    directly; `create_task` queues the coro for the next iteration."""
    loop = getattr(adapter, "_loop", None)
    if loop is None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
    if loop is None or not loop.is_running():
        logger.debug("chat4000 hook: no running loop, dropping frame")
        coro.close()
        return
    try:
        loop.create_task(coro)
    except Exception as exc:
        logger.debug("chat4000 hook: schedule failed: %s", exc)
        coro.close()


def on_pre_tool_call(
    *,
    tool_name: str = "",
    args: Any = None,
    task_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    **_: Any,
) -> None:
    """Hermes calls this synchronously from agent.tool_executor BEFORE
    a tool runs. We emit a `tool_start` frame to the iOS app so the
    expandable bubble appears immediately."""
    logger.info(
        "chat4000.pre_tool_call: tool=%s session=%s task=%s tool_call_id=%s map_has=%s",
        tool_name, session_id, task_id, tool_call_id,
        _SESSION_PLATFORM.get(session_id or task_id),
    )
    adapter = _adapter_for_session(session_id or task_id)
    if adapter is None or adapter._tool_dispatcher is None:
        logger.info(
            "chat4000.pre_tool_call: no adapter (active=%d, map=%s)",
            len(_ACTIVE_ADAPTERS), dict(_SESSION_PLATFORM),
        )
        return

    queue_key = (task_id or session_id, tool_name)

    # Diagnostic: how many adapters/dispatchers are live + which transport
    # will receive the send. If active>1, two platform instances were
    # registered and both ToolCallDispatchers will see calls.
    logger.info(
        "chat4000.pre_tool_call: adapter id=%s dispatcher=%s active_count=%d transport=%s",
        id(adapter), id(adapter._tool_dispatcher),
        len(_ACTIVE_ADAPTERS),
        id(getattr(adapter, "_transport", None)),
    )

    # Look up the tool's emoji from Hermes' central registry. This is the
    # same source Telegram, IRC and the CLI all use (`agent.display`),
    # so the iOS app gets the same per-tool icon vocabulary out of the
    # box — skill_view → 📚, todo → 📋, cronjob → ⏰, bash → 🛠️, etc.
    icon = ""
    try:
        from agent.display import get_tool_emoji  # type: ignore[import-not-found]
        icon = get_tool_emoji(tool_name, default="")
    except Exception as exc:
        logger.info("chat4000.pre_tool_call: get_tool_emoji failed: %s", exc)
    logger.info(
        "chat4000.pre_tool_call: resolved icon=%r for tool=%s", icon, tool_name,
    )

    async def _emit() -> None:
        our_id = await adapter._tool_dispatcher.on_tool_start(
            name=tool_name, args=args or {}, icon=icon
        )
        _PENDING_TOOL_CALLS.setdefault(queue_key, []).append((adapter, our_id))

    _schedule_async(adapter, _emit())


def on_post_tool_call(
    *,
    tool_name: str = "",
    args: Any = None,
    result: Any = None,
    task_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    **_: Any,
) -> None:
    """Hermes calls this synchronously from agent.tool_executor AFTER
    a tool finishes. We emit `tool_end` with status + result."""
    logger.info(
        "chat4000.post_tool_call: tool=%s session=%s task=%s tool_call_id=%s",
        tool_name, session_id, task_id, tool_call_id,
    )
    queue_key = (task_id or session_id, tool_name)
    queue = _PENDING_TOOL_CALLS.get(queue_key)
    if queue:
        adapter, our_id = queue.pop(0)
        if not queue:
            _PENDING_TOOL_CALLS.pop(queue_key, None)
    else:
        # pre_tool_call didn't fire (e.g. session predates plugin load) —
        # nothing to close. Emitting a tool_end with a fabricated id
        # would create a ghost bubble on the client, so just skip.
        logger.info(
            "chat4000.post_tool_call: no pending pre for %s, skipping",
            queue_key,
        )
        return

    if adapter._tool_dispatcher is None:
        return

    if adapter._tool_dispatcher is None:
        return

    # Hermes returns the tool's raw output string; treat shell-error-style
    # prefixes as failures so the bubble renders with the red badge.
    result_text = result if isinstance(result, str) else (
        "" if result is None else str(result)
    )
    status = "failed" if result_text.startswith(("[error", "Error", "Traceback")) else "done"

    async def _emit() -> None:
        await adapter._tool_dispatcher.on_tool_end(
            our_id, status=status, result=result_text
        )

    _schedule_async(adapter, _emit())


def register_plugin_hooks(ctx) -> None:
    """Wire pre/post_tool_call hooks into Hermes' plugin system. Called
    from the plugin's `register(ctx)` at discovery time. Failure is
    logged but non-fatal — the platform itself still works, you just
    won't get tool bubbles."""
    try:
        ctx.register_hook("on_session_start", on_session_start)
        ctx.register_hook("on_session_end", on_session_end)
        ctx.register_hook("pre_llm_call", on_pre_llm_call)
        ctx.register_hook("post_llm_call", on_post_llm_call)
        ctx.register_hook("pre_tool_call", on_pre_tool_call)
        ctx.register_hook("post_tool_call", on_post_tool_call)
        logger.info("chat4000: tool-call hooks registered")
    except Exception as exc:
        logger.warning("chat4000: hook registration failed: %s", exc)
