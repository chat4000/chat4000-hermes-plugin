"""Pure tool-activity transcript detection (protocol E — "Push signaling").

The Hermes host narrates tool activity by handing the adapter plain `m.text`
strings such as:

    💻 terminal: "python3 - <<'PY' …"
    📚 skill_view: "news-research-monitoring"
    ⚠️ Dangerous command…
    ⏳ Working…

These are NOT the turn's final answer, so per protocol E they must NEVER be
push-eligible (every non-final event carries `chat4000.push: false`; only the
single final answer edit may be `true`). Yet they arrive through the SAME
`send()` / `edit_message()` text path as a real answer, and when they land while
the room has no active turn the adapter would otherwise mark them `final=True`
→ `push=True` and wake the user with a banner.

This module ports the iOS client's `isPureToolTranscript` predicate
(clawconnect-client-swift … RoomViewModel.swift `isPureToolTranscript` /
`isToolTranscriptLine` / `isLikelyToolName`) verbatim so the plugin and the
client agree on EXACTLY what counts as a "pure tool transcript". Keeping the two
in lockstep means a string the client would hide is a string the plugin will
never push.

Matching rule (per non-empty, trimmed line): split into at most two
whitespace-separated parts (an icon/prefix token and the rest); the rest must
start with a tool name (terminated by ':', '.', or whitespace) that looks like a
tool name, and the terminator run must be ':' or '...'. A text is a pure tool
transcript only when EVERY non-empty line matches.
"""

from __future__ import annotations

# Bare single-word tool names that have no underscore/dot/dash to mark them.
# Mirrors the client's `knownSimpleNames` set EXACTLY.
_KNOWN_SIMPLE_NAMES = frozenset({"bash", "python", "terminal", "todo", "cronjob"})


def is_pure_tool_transcript(text: str) -> bool:
    """True iff `text` is composed ENTIRELY of tool-activity narration lines.

    Empty / whitespace-only text is NOT a transcript (returns False) — a genuine
    final answer is never empty here, and an empty body is handled upstream.
    Mirrors the Swift `isPureToolTranscript`.
    """
    lines = [
        stripped
        for raw in text.splitlines()
        if (stripped := raw.strip())
    ]
    if not lines:
        return False
    return all(_is_tool_transcript_line(line) for line in lines)


def _is_tool_transcript_line(line: str) -> bool:
    """Mirrors the Swift `isToolTranscriptLine`.

    Split on the first whitespace into [prefix, rest]; rest must start with a
    plausible tool name terminated by ':' or '...'.
    """
    parts = line.split(None, 1)
    if len(parts) != 2:
        return False
    rest = parts[1]

    name_end = _first_terminator_index(rest)
    if name_end is None:
        return False

    name = rest[:name_end]
    if not _is_likely_tool_name(name):
        return False

    suffix = rest[name_end:]
    return suffix.startswith(":") or suffix.startswith("...")


def _first_terminator_index(rest: str) -> int | None:
    """Index of the first ':' , '.' , or whitespace char in `rest`, else None.

    Mirrors the Swift `isToolNameTerminator` used via `firstIndex(where:)`.
    """
    for index, character in enumerate(rest):
        if character in (":", ".") or character.isspace():
            return index
    return None


def _is_likely_tool_name(name: str) -> bool:
    """Mirrors the Swift `isLikelyToolName`: a known simple name, or any name
    containing '_', '.', or '-' (so multi-token identifiers like `skill_view`,
    `web.search`, `news-research` qualify)."""
    if not name:
        return False
    if name in _KNOWN_SIMPLE_NAMES:
        return True
    return "_" in name or "." in name or "-" in name
