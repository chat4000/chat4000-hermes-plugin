"""TurnWriter tool events — chat4000.tool shape (protocol E / client contract).

tool_start carries chat4000.turn_id ENCRYPTED in the content (the turn link);
tool_end is a standard m.replace edit whose m.new_content holds the updated
chat4000.tool object. tool_id is stable across start and end.
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


async def test_tool_start_embeds_turn_id_no_cleartext_relation() -> None:
    c = _FakeCrypto()
    await _writer(c).tool_start("!r", "$anchor", tool_id="t1", name="bash", args="{}", icon="x")
    s = c.sends[-1]
    assert s["event_type"] == "chat4000.tool"
    assert s["content"]["msgtype"] == "chat4000.tool"
    # Turn link is encrypted-in-content, NOT a cleartext m.relates_to.
    assert s["content"]["chat4000.turn_id"] == "$anchor"
    assert s["relates_to"] is None
    assert s["content"]["chat4000.tool"]["tool_id"] == "t1"
    assert s["content"]["chat4000.tool"]["status"] == "running"
    assert s["push"] is False


async def test_tool_end_is_mreplace_edit_with_new_content() -> None:
    c = _FakeCrypto()
    await _writer(c).tool_end(
        "!r", "$toolev", tool_id="t1", name="bash", args="{}",
        status="done", result="ok", duration_ms=5,
    )
    s = c.sends[-1]
    assert s["relates_to"] == {"rel_type": "m.replace", "event_id": "$toolev"}
    new_tool = s["content"]["m.new_content"]["chat4000.tool"]
    assert new_tool["tool_id"] == "t1"  # stable across start/end
    assert new_tool["status"] == "done"
    assert new_tool["result"] == "ok"
    assert s["push"] is False
