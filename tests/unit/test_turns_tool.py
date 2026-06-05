"""TurnWriter tool events — chat4000.tool START-ONLY shape (protocol E).

One event per tool, sent at start: content {tool_id, name, icon?}, push:false,
NO chat4000.turn_id, NO m.replace edit, NO args/status/result/duration. The event
is never updated — the client renders a static chip.
"""

from __future__ import annotations

from typing import Any

from chat4000_hermes_plugin.matrix.turns import TurnWriter


class _FakeCrypto:
    def __init__(self) -> None:
        self.sends: list[dict[str, Any]] = []

    async def send_room_event(
        self,
        room_id: str,
        event_type: str,
        content: dict[str, Any],
        members: list[str],
        *,
        push: bool | None = None,
        relates_to: dict[str, Any] | None = None,
        txn_id: str | None = None,
    ) -> str:
        self.sends.append(
            {"event_type": event_type, "content": content, "push": push, "relates_to": relates_to}
        )
        return "$ev"


def _writer(crypto: _FakeCrypto) -> TurnWriter:
    return TurnWriter(crypto, object(), ["@u:hs"])  # type: ignore[arg-type]


async def test_tool_start_is_start_only_static_event() -> None:
    c = _FakeCrypto()
    await _writer(c).tool_start("!r", tool_id="t1", name="skill_view", icon="📚")
    s = c.sends[-1]
    assert s["event_type"] == "chat4000.tool"
    assert s["content"]["msgtype"] == "chat4000.tool"
    tool = s["content"]["chat4000.tool"]
    assert tool == {"tool_id": "t1", "name": "skill_view", "icon": "📚"}
    # START-only: no turn link, no edit, no completion fields, push:false.
    assert "chat4000.turn_id" not in s["content"]
    assert "m.new_content" not in s["content"]
    assert s["relates_to"] is None
    for k in ("args", "status", "result", "duration_ms"):
        assert k not in tool
    assert s["push"] is False


async def test_tool_start_omits_icon_when_empty_and_caps_sizes() -> None:
    c = _FakeCrypto()
    await _writer(c).tool_start("!r", tool_id="t1", name="x" * 100, icon="")
    tool = c.sends[-1]["content"]["chat4000.tool"]
    assert "icon" not in tool  # empty icon → omitted entirely
    assert len(tool["name"]) == 64  # name capped to 64 (TOOL_NAME_MAX)
