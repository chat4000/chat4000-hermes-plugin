"""v2 plugin_hooks — tool lifecycle → external_tool_* on the active adapter.

The hooks are sync and schedule async emits on the adapter's loop, so each test
drains the loop a few ticks. Module-global state is cleared around each test.
"""

from __future__ import annotations

import asyncio

import chat4000_hermes_plugin.plugin_hooks as h


class FakeAdapter:
    def __init__(self, loop):
        self._connected = True
        self._loop = loop
        self.started: list = []
        self.ended: list = []

    async def external_tool_start(self, name, args, icon=""):
        tid = f"tid-{name}"
        self.started.append((name, args, icon, tid))
        return tid

    async def external_tool_end(self, tool_id, *, status="done", result=""):
        self.ended.append((tool_id, status, result))


async def _drain():
    for _ in range(6):
        await asyncio.sleep(0)


def _clear():
    h._SESSION_PLATFORM.clear()
    h._PENDING_TOOL_CALLS.clear()


async def test_pre_then_post_routes_to_adapter():
    a = FakeAdapter(asyncio.get_running_loop())
    h.register_active_adapter(a)
    _clear()
    h._SESSION_PLATFORM["s1"] = "chat4000"
    try:
        h.on_pre_tool_call(tool_name="bash", args={"cmd": "ls"}, task_id="t1", session_id="s1")
        await _drain()
        assert a.started and a.started[0][0] == "bash"
        h.on_post_tool_call(tool_name="bash", result="ok", task_id="t1", session_id="s1")
        await _drain()
        assert a.ended == [("tid-bash", "done", "ok")]
    finally:
        h.deregister_active_adapter(a)
        _clear()


async def test_error_result_marks_failed():
    a = FakeAdapter(asyncio.get_running_loop())
    h.register_active_adapter(a)
    _clear()
    h._SESSION_PLATFORM["s1"] = "chat4000"
    try:
        h.on_pre_tool_call(tool_name="bash", args={}, task_id="t1", session_id="s1")
        await _drain()
        h.on_post_tool_call(tool_name="bash", result="Error: boom", task_id="t1", session_id="s1")
        await _drain()
        assert a.ended[0][1] == "failed"
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


def test_schedule_async_from_worker_thread_runs_on_loop() -> None:
    """Regression: pre/post_tool_call hooks fire on Hermes' executor thread (no
    running loop there). _schedule_async must hand the coroutine to the gateway
    loop via run_coroutine_threadsafe, NOT drop it with a cross-thread create_task.
    On the old code the coroutine was silently closed -> zero chat4000.tool events."""
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


async def test_orphan_sweep_closes_bubble():
    a = FakeAdapter(asyncio.get_running_loop())
    h.register_active_adapter(a)
    _clear()
    h._SESSION_PLATFORM["s1"] = "chat4000"
    try:
        h.on_pre_tool_call(tool_name="todo", args={}, task_id="s1", session_id="s1")
        await _drain()
        # post_tool_call never fires for intercepted tools → sweep closes it.
        h.on_post_llm_call(session_id="s1", platform="chat4000")
        await _drain()
        assert a.ended == [("tid-todo", "done", "")]
        assert not h._PENDING_TOOL_CALLS
    finally:
        h.deregister_active_adapter(a)
        _clear()
