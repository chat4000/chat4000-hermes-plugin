"""Pure tool-activity transcript predicate (protocol E — "Push signaling").

Ports + locks the iOS client's `isPureToolTranscript` behavior
(clawconnect-client-swift … RoomViewModel.swift). The plugin and client MUST
agree on what counts as "pure tool transcript" so a string the client hides is a
string the plugin never pushes.
"""

from __future__ import annotations

import pytest

from chat4000_hermes_plugin.matrix.tool_transcript import is_pure_tool_transcript


@pytest.mark.parametrize(
    "text",
    [
        '💻 terminal: "python3 - <<\'PY\' …"',
        '📚 skill_view: "news-research-monitoring"',
        "📚 skill_view: news-research-monitoring",
        "💻 bash: ls -la",
        "💻 python: print(1)",
        "🔧 news-research: top stories",
        "⚠️ skill_view: dangerous",
        # multi-line: every line is tool narration → still pure
        "💻 terminal: pwd\n📚 skill_view: docs\n🔧 news-research: x",
        # the '...' working/ellipsis form
        "💻 terminal...",
    ],
)
def test_pure_tool_transcripts_detected(text: str) -> None:
    assert is_pure_tool_transcript(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "",  # empty is never a transcript
        "   \n  ",  # whitespace-only
        "A. Ynet / A1. the top headline is the budget vote",  # real prose answer
        "Here is the answer: 42",  # 'answer' is no underscore/dot/dash, not a tool name
        "Sure, I ran terminal and got the result.",  # prose mentioning a tool
        "💻 terminal: ls\nHere is what I found.",  # mixed → NOT pure (second line is prose)
        "no_prefix_just_one_token",  # single token, no icon/name split
        "💻 noname",  # no ':' or '...' terminator after the name
    ],
)
def test_non_transcripts_rejected(text: str) -> None:
    assert is_pure_tool_transcript(text) is False
