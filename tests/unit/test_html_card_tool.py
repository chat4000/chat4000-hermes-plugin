from __future__ import annotations

import asyncio
import json
from typing import Any

import chat4000_hermes_plugin.plugin_hooks as hooks
from chat4000_hermes_plugin.html_card_tool import (
    HTML_CARD_TOOL_NAME,
    HTML_CARD_TOOL_SCHEMA,
    HTML_CARD_TOOLSET,
    register_html_card_tool,
    send_html_card_tool,
)


class _FakeAdapter:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._connected = True
        self._loop = loop
        self._room_by_session = {"s1": "!r"}
        self._session_by_room = {"!r": "s1"}
        self.sent: list[tuple[str, str, str]] = []

    def _room_for_session(self, session_id: str) -> str | None:
        return self._room_by_session.get(session_id)

    async def external_html_card(self, html: str, *, room: str = "", session_id: str = "") -> str:
        self.sent.append((room, session_id, html))
        return "$card"


class _FakeContext:
    def __init__(self) -> None:
        self.tool: dict[str, Any] | None = None

    def register_tool(self, **kwargs: Any) -> None:
        self.tool = kwargs


def test_register_html_card_tool_uses_hermes_plugin_tool_api() -> None:
    ctx = _FakeContext()

    register_html_card_tool(ctx)

    assert ctx.tool is not None
    assert ctx.tool["name"] == HTML_CARD_TOOL_NAME
    assert ctx.tool["toolset"] == HTML_CARD_TOOLSET
    assert ctx.tool["schema"] == HTML_CARD_TOOL_SCHEMA
    assert ctx.tool["handler"] is send_html_card_tool
    assert ctx.tool["is_async"] is True


async def test_send_html_card_tool_sends_only_inside_chat4000_turn(monkeypatch) -> None:
    loop = asyncio.get_running_loop()
    adapter = _FakeAdapter(loop)
    hooks.register_active_adapter(adapter)  # type: ignore[arg-type]
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "chat4000")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "!r")
    monkeypatch.setenv("HERMES_SESSION_ID", "s1")
    try:
        result = json.loads(await send_html_card_tool({"html": "<article>Done</article>"}))
    finally:
        hooks.deregister_active_adapter(adapter)  # type: ignore[arg-type]

    assert result == {"ok": True, "sent": True, "event_id": "$card"}
    assert adapter.sent == [("!r", "s1", "<article>Done</article>")]


async def test_send_html_card_tool_noops_outside_chat4000_turn(monkeypatch) -> None:
    loop = asyncio.get_running_loop()
    adapter = _FakeAdapter(loop)
    hooks.register_active_adapter(adapter)  # type: ignore[arg-type]
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "!r")
    monkeypatch.setenv("HERMES_SESSION_ID", "s1")
    try:
        result = json.loads(await send_html_card_tool({"html": "<article>Done</article>"}))
    finally:
        hooks.deregister_active_adapter(adapter)  # type: ignore[arg-type]

    assert result == {"ok": True, "sent": False, "reason": "not_chat4000_turn"}
    assert adapter.sent == []
