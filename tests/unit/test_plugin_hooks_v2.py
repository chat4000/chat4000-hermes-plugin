"""v2 plugin_hooks — START-ONLY tool lifecycle.

pre_tool_call emits ONE chat4000.tool START event (via the active adapter's
external_tool_start); there is no post_tool_call END and no post_llm_call flush.
The hooks are sync and schedule the async emit on the adapter's loop, so each test
drains the loop a few ticks. Module-global state is cleared around each test.
"""

from __future__ import annotations

import asyncio

import chat4000_hermes_plugin.plugin_hooks as h
from chat4000_hermes_plugin.html_card_tool import HTML_CARD_TOOL_NAME


class FakeAdapter:
    def __init__(self, loop):
        self._connected = True
        self._loop = loop
        self._active_room = "!r"
        self.started: list = []

    async def external_tool_start(self, name, args=None, icon="", session_id="", room=""):
        tid = f"tid-{name}"
        self.started.append((name, args, icon, tid, session_id, room))
        return tid

    def _room_for_session(self, session_id):
        return self._active_room


async def _drain():
    for _ in range(6):
        await asyncio.sleep(0)


def _clear():
    h._SESSION_PLATFORM.clear()


async def test_pre_tool_call_emits_one_start():
    a = FakeAdapter(asyncio.get_running_loop())
    h.register_active_adapter(a)
    _clear()
    h._SESSION_PLATFORM["s1"] = "chat4000"
    try:
        h.on_pre_tool_call(tool_name="bash", args={"cmd": "ls"}, task_id="t1", session_id="s1")
        await _drain()
        assert len(a.started) == 1  # exactly one START, no END/flush
        assert a.started[0][0] == "bash"
        assert a.started[0][4] == "s1"  # firing session threaded through
    finally:
        h.deregister_active_adapter(a)
        _clear()


async def test_pre_tool_call_routes_by_contextvar_room(monkeypatch):
    """The tool's room comes from Hermes' task-local chat contextvar (read
    synchronously in the hook), so concurrent turns can't cross."""
    a = FakeAdapter(asyncio.get_running_loop())
    h.register_active_adapter(a)
    _clear()
    h._SESSION_PLATFORM["s1"] = "chat4000"
    monkeypatch.setattr(h, "_current_chat_id", lambda: "!ctxroom")
    try:
        h.on_pre_tool_call(tool_name="bash", args={}, task_id="t1", session_id="s1")
        await _drain()
        assert a.started[0][5] == "!ctxroom"  # routed by the LIVE turn's room
    finally:
        h.deregister_active_adapter(a)
        _clear()


async def test_non_chat4000_session_is_ignored():
    a = FakeAdapter(asyncio.get_running_loop())
    h.register_active_adapter(a)
    _clear()  # s9 NOT registered as chat4000
    try:
        h.on_pre_tool_call(tool_name="bash", args={}, task_id="t9", session_id="s9")
        await _drain()
        assert a.started == []
    finally:
        h.deregister_active_adapter(a)
        _clear()


async def test_html_card_tool_does_not_emit_visible_tool_chip():
    a = FakeAdapter(asyncio.get_running_loop())
    h.register_active_adapter(a)
    _clear()
    h._SESSION_PLATFORM["s1"] = "chat4000"
    try:
        h.on_pre_tool_call(
            tool_name=HTML_CARD_TOOL_NAME,
            args={"html": "<article>x</article>"},
            session_id="s1",
        )
        await _drain()
        assert a.started == []
    finally:
        h.deregister_active_adapter(a)
        _clear()


def test_schedule_async_from_worker_thread_runs_on_loop() -> None:
    """Regression: pre_tool_call fires on Hermes' executor thread (no running loop
    there). _schedule_async must hand the coroutine to the gateway loop via
    run_coroutine_threadsafe, NOT drop it with a cross-thread create_task."""
    import threading

    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    try:
        ran = threading.Event()

        class _A:
            _loop = loop

        async def _coro() -> None:
            ran.set()

        h._schedule_async(_A(), _coro())  # called from a non-loop thread
        assert ran.wait(2.0), "coroutine was dropped instead of scheduled on the loop"
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(2.0)
        loop.close()
