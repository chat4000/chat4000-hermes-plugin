"""Hermes plugin-level tool hooks → chat4000.tool events (v2 / Matrix).

Tool calls are START-ONLY (protocol E): ONE `chat4000.tool` event per tool, sent
when it starts, never updated. There is no END, no `m.replace` edit, no
result/status/duration, and therefore no pre↔post correlation, no FIFO queue, and
no orphan-flush — tool events only hook `pre_tool_call`.

Mechanics:
  - Adapters self-register on `__init__` (weakref).
  - `on_session_start` / `pre_llm_call` record which sessions are chat4000.
  - `pre_tool_call` reads the firing turn's room from Hermes' task-local chat
    contextvar (synchronously, on the executor thread — correct under concurrency)
    and emits one START event via the active adapter's `external_tool_start`.
  - `post_llm_call` (first exchanges only) starts a read-only state.db poll that
    picks up the host's auto-generated session title → Matrix room name.
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

# Host auto-title pickup: Hermes generates a session title on a daemon thread
# after the first exchange (LLM call, 30s host timeout) and stores it in
# state.db — it never delivers it to platform adapters. We poll the DB
# read-only until the title lands, then apply it as the Matrix room name.
_TITLE_POLL_INTERVAL_S = 2.0  # seconds between read-only state.db reads
_TITLE_POLL_BUDGET_S = 60.0  # total budget; covers the host's 30s LLM timeout

# session_id → poller in flight (dedupe: repeated hook fires must not stack).
_TITLE_POLLS_ACTIVE: set[str] = set()
# room_id → a host title was already applied (never re-poll a titled room).
_TITLE_APPLIED_ROOMS: set[str] = set()


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


def connected_adapter_for_room(room: str, session_id: str = "") -> Chat4000MatrixAdapter | None:
    """Return the connected adapter that owns this Chat4000 turn room."""
    candidates = [
        adapter for adapter in list(_ACTIVE_ADAPTERS) if getattr(adapter, "_connected", False)
    ]
    if not room or not candidates:
        return None

    for adapter in candidates:
        resolver = getattr(adapter, "_room_for_session", None)
        if callable(resolver) and session_id and resolver(session_id) == room:
            return adapter

    for adapter in candidates:
        by_session = getattr(adapter, "_room_by_session", {})
        if isinstance(by_session, dict) and room in by_session.values():
            return adapter
        by_room = getattr(adapter, "_session_by_room", {})
        if isinstance(by_room, dict) and room in by_room:
            return adapter

    return candidates[0] if len(candidates) == 1 else None


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
        _TITLE_POLLS_ACTIVE.discard(session_id)


# ─── host auto-title pickup (post_llm_call → state.db poll) ─────────────────


def _load_session_db() -> Any:  # noqa: ANN401  # host class is untyped (Any)
    """Lazily import the host's SessionDB class. `hermes_state` only exists
    inside the Hermes host process — unit tests/CI don't have it — so an
    ImportError means "not in a host" and the caller gives up silently."""
    try:
        from hermes_state import SessionDB
    except ImportError:
        return None
    return SessionDB


async def _poll_host_title(adapter: Chat4000MatrixAdapter, session_id: str, room: str) -> None:
    """Poll state.db (read-only) until the host's auto-titler stores a title for
    `session_id`, then apply it as `room`'s name. Runs on the adapter's gateway
    loop; the sqlite calls are blocking, so they go through asyncio.to_thread.
    Gives up silently on budget exhaustion (the host titler itself may have
    failed); unexpected errors are reported once and never raised."""
    db: Any = None
    try:
        session_db_cls = _load_session_db()
        if session_db_cls is None:
            logger.debug("chat4000 title poll: hermes_state unavailable, skipping")
            return
        db = await asyncio.to_thread(session_db_cls, read_only=True)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _TITLE_POLL_BUDGET_S
        while True:
            title = await asyncio.to_thread(db.get_session_title, session_id)
            if title:
                await adapter._apply_host_session_title(room, title)
                _TITLE_APPLIED_ROOMS.add(room)
                return
            if loop.time() >= deadline:
                logger.debug(
                    "chat4000 title poll: no host title for session=%s within budget", session_id
                )
                return
            await asyncio.sleep(_TITLE_POLL_INTERVAL_S)
    except Exception as exc:  # noqa: BLE001  # hook path: report once, never raise
        from .error_log import dump_chat4000_trace

        dump_chat4000_trace(
            "plugin_hooks.title_poll", exc, {"session_id": session_id, "room": room}
        )
    finally:
        if db is not None:
            try:
                db.close()
            except Exception as exc:  # noqa: BLE001  # ro-connection close is best-effort
                logger.debug("chat4000 title poll: SessionDB close failed: %s", exc)
        # On success _TITLE_APPLIED_ROOMS blocks re-polls; otherwise a later
        # hook fire (second exchange — host retries titling too) may retry.
        _TITLE_POLLS_ACTIVE.discard(session_id)


def on_post_llm_call(
    *,
    session_id: str = "",
    conversation_history: list[Any] | None = None,
    platform: str = "",
    **_: object,
) -> None:
    """Fired once per turn after the tool-calling loop completes. On a chat4000
    session's FIRST exchanges, start ONE title poller for the session's room.
    Mirrors the host titler's own heuristic (title_generator.maybe_auto_title:
    only when the history holds <= 2 user messages) so we never poll for a turn
    the host won't title."""
    if not session_id:
        return
    if (platform or "").strip().lower() == "chat4000":
        _SESSION_PLATFORM.setdefault(session_id, "chat4000")
    adapter = _adapter_for_session(session_id)
    if adapter is None:
        return
    user_msg_count = sum(
        1
        for m in (conversation_history or [])
        if isinstance(m, dict) and m.get("role") == "user"
    )
    if user_msg_count > 2:
        return
    room = adapter._room_for_session(session_id)
    if not room:
        return
    if session_id in _TITLE_POLLS_ACTIVE or room in _TITLE_APPLIED_ROOMS:
        return
    _TITLE_POLLS_ACTIVE.add(session_id)
    _schedule_async(adapter, _poll_host_title(adapter, session_id, room))


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
    from .html_card_tool import HTML_CARD_TOOL_NAME

    if tool_name == HTML_CARD_TOOL_NAME:
        return

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
        ctx.register_hook("post_llm_call", on_post_llm_call)
        logger.info("chat4000: v2 tool-call + title hooks registered")
    except Exception as exc:  # noqa: BLE001
        # Hook registration is optional (tool bubbles); a host that lacks
        # register_hook must not crash the plugin. Report once, then continue.
        from .error_log import dump_chat4000_trace

        logger.warning("chat4000: hook registration failed: %s", exc)
        dump_chat4000_trace("plugin_hooks.register", exc)
