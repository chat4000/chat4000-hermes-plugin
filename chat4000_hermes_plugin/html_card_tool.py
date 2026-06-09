"""Chat4000-only Hermes tool for typed HTML-card final answers."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

HTML_CARD_TOOL_NAME = "chat4000_send_html_card"
HTML_CARD_TOOLSET = "chat4000"

HTML_CARD_TOOL_SCHEMA: dict[str, Any] = {
    "name": HTML_CARD_TOOL_NAME,
    "description": (
        "Send a complete HTML card as the final answer in the current Chat4000 "
        "turn. Use only when the HTML is complete; do not also send a text final "
        "answer."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "html": {
                "type": "string",
                "description": "Complete card HTML for the Chat4000 client to render.",
            }
        },
        "required": ["html"],
        "additionalProperties": False,
    },
}


def register_html_card_tool(ctx: Any) -> None:  # noqa: ANN401  # Hermes plugin context
    ctx.register_tool(
        name=HTML_CARD_TOOL_NAME,
        toolset=HTML_CARD_TOOLSET,
        schema=HTML_CARD_TOOL_SCHEMA,
        handler=send_html_card_tool,
        is_async=True,
        description="Send a Chat4000 typed HTML-card final answer.",
    )


async def send_html_card_tool(args: dict[str, Any], **kwargs: object) -> str:
    html = args.get("html")
    if not isinstance(html, str) or not html:
        return _json_result(ok=False, sent=False, error="html must be a non-empty string")

    context = _current_chat4000_context(kwargs)
    if context is None:
        return _json_result(ok=True, sent=False, reason="not_chat4000_turn")

    from .plugin_hooks import connected_adapter_for_room

    adapter = connected_adapter_for_room(context.room_id, context.session_id)
    if adapter is None:
        return _json_result(ok=False, sent=False, error="chat4000 adapter unavailable")

    event_id = await _run_on_adapter_loop(
        adapter,
        adapter.external_html_card(html, room=context.room_id, session_id=context.session_id),
    )
    if not event_id:
        return _json_result(ok=False, sent=False, error="html card was not sent")
    return _json_result(ok=True, sent=True, event_id=event_id)


class _Chat4000Context:
    def __init__(self, *, room_id: str, session_id: str) -> None:
        self.room_id = room_id
        self.session_id = session_id


def _current_chat4000_context(kwargs: dict[str, object]) -> _Chat4000Context | None:
    platform = _session_value("HERMES_SESSION_PLATFORM").strip().lower()
    room_id = _session_value("HERMES_SESSION_CHAT_ID").strip()
    if platform != "chat4000" or not room_id:
        return None

    session_id = _session_value("HERMES_SESSION_ID").strip()
    if not session_id:
        session_id = str(kwargs.get("session_id") or kwargs.get("task_id") or "")
    return _Chat4000Context(room_id=room_id, session_id=session_id)


def _session_value(name: str) -> str:
    try:
        from gateway.session_context import get_session_env
    except ModuleNotFoundError:
        return os.environ.get(name, "")
    return get_session_env(name, "") or ""


async def _run_on_adapter_loop(adapter: Any, coro: Any) -> str:  # noqa: ANN401
    loop = getattr(adapter, "_loop", None)
    if loop is None or not loop.is_running():
        coro.close()
        return ""

    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None

    if current_loop is loop:
        result = await coro
    else:
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        result = await asyncio.wrap_future(future)
    return result if isinstance(result, str) else ""


def _json_result(
    *,
    ok: bool,
    sent: bool,
    event_id: str | None = None,
    reason: str | None = None,
    error: str | None = None,
) -> str:
    payload: dict[str, Any] = {"ok": ok, "sent": sent}
    if event_id:
        payload["event_id"] = event_id
    if reason:
        payload["reason"] = reason
    if error:
        payload["error"] = error
    return json.dumps(payload, ensure_ascii=False)
