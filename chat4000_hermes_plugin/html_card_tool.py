"""final_card tool (native rich final answer surface for chat4000 sessions).

The tool description below IS the prompt that makes the model pick this tool
and produce on-brand cards. It is assembled from named sections so each
section's token footprint stays measurable and editable on its own. The card
catalog + ranking these sections came from lives at
/tmp/chat4000-card-examples-v3-ranked.html (picked: #03 #04 #05 #06 #08 #09
#17 #18 #19 #20 #25b #25d; code examples: #03 #04 #17 #19 #20).
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

HTML_CARD_TOOL_NAME = "final_card"
HTML_CARD_TOOLSET = "chat4000"

_CORE_RULE = (
    "Use final_card as the preferred final-answer surface in chat4000 when the "
    "answer has meaningful structure, comparison, status, visual hierarchy, or "
    "scan-worthy information. This is the native rich card surface in the "
    "chat4000 timeline. The card replaces the text answer: call this tool once "
    "per turn, with finished self-contained HTML (never partial or streamed), "
    "and do not also send a text final answer. "
    "final_card is the ONLY way to show a card: a card is displayed ONLY by "
    "calling this tool. Do NOT put the card HTML in your text reply and do NOT "
    "write it to a file — raw HTML in a text answer renders as literal text (it "
    "never becomes a card), and a written .html file is never shown to the user. "
    "To show a card you MUST call final_card with the HTML."
)

_WHEN_TO_USE = """\
Reach for final_card more often than plain text when layout helps: comparison
matrices, pros/cons, ranked recommendations, plans, timelines, schedules, status
dashboards, checklists, metric summaries, knowledge panels, weather, travel,
events, agendas, to-do lists, and dense multi-section answers.

Default toward final_card for structured final answers unless the user asked for
plain text or the answer is obviously better as copyable text. The goal is not
decoration; the goal is a faster, clearer final answer in the chat4000 native UI.

Do not use final_card for one-liners, quick facts, yes/no answers, casual
conversation, pure prose explanations, code-only answers, shell commands, raw
logs, stack traces, or text the user is likely to copy. When in doubt between a
useful card and a wall of text, choose the card."""

_RENDER_CONTEXT = """\
RENDER CONTEXT — the card renders as a bubble in the chat4000 iOS/macOS chat
timeline, inside a sandboxed WebView: CSS and JS run, but there is NO NETWORK
ACCESS. No external fonts, no remote images, no CDN libraries — inline everything;
use emoji, inline SVG, or pure CSS for graphics. The chat behind the card is
near-black (#0F0F0F): leave html/body transparent and draw your own surface (the
TEMPLATE below provides it). Design for phone width: max-width ~420px, 12-13px
mono body text, generous padding.

ROBUSTNESS — the card width varies (~340–420px); content must NEVER overflow
horizontally or wrap awkwardly. Keep pills, badges, and short labels on one line
(white-space:nowrap). Let long text wrap (overflow-wrap:anywhere). Avoid fixed
pixel widths over ~300px and long unbreakable strings. Every row must survive a
narrow width without clipping, a single word breaking the layout, or a 2-word
label wrapping to two lines."""

_STYLE_GUIDE = """\
STYLE GUIDE — dark, minimal, terminal/monospace; a developer tool, not a consumer
app. Surfaces: card #141414, raised inner panels #1A1A1A, 1px borders
rgba(255,255,255,0.08) (0.14 for emphasis). Text: #FFFFFF titles, #E0E0E0 body,
#9CA3AF labels, #666666 muted/timestamps. Accents sparingly, on a monochrome base:
PINK #EC4899 is the brand hero (bright #F472B6 for highlights); BLUE #53BDEB is
secondary (links, info, "up/ok"). Typography: monospace everywhere —
ui-monospace,"SF Mono",Menlo,monospace; weights 400 body / 500 labels / 600-700
titles. Shape: radius 8px chips, 12-14px cards; pills are full capsules; padding
16-24px card, 12-16px inner. Buttons: primary = white bg + black text; accent =
pink bg + white text; secondary = transparent + 1px subtle border + #9CA3AF text.
DO: thin borders, tight grids, emoji or inline-SVG icons, gradients only built
from pink/blue on dark. DON'T: light/white backgrounds, serif or rounded fonts,
green/orange/red accents, rainbow gradients, remote assets, heavy shadows."""

_CARD_TEMPLATE = """\
TEMPLATE — start every card with this exact style block + root; put the content
inside .c4k and reuse its CSS variables and helper classes:
<style>
.c4k{--raised:#1A1A1A;--border:rgba(255,255,255,.08);--border-hi:rgba(255,255,255,.14);
--text:#FFF;--body:#E0E0E0;--label:#9CA3AF;--muted:#666;--pink:#EC4899;
--pink-hi:#F472B6;--blue:#53BDEB;background:#141414;border:1px solid var(--border);
border-radius:14px;padding:20px;max-width:420px;color:var(--body);
font:13px/1.5 ui-monospace,"SF Mono",Menlo,monospace;box-sizing:border-box;overflow-wrap:anywhere}
.c4k *{box-sizing:border-box;min-width:0}
.c4k svg,.c4k img{max-width:100%;height:auto}
.c4k .k{color:var(--label);font-size:11px;font-weight:500;text-transform:uppercase;
letter-spacing:.08em}
.c4k .row{display:flex;align-items:center;justify-content:space-between;gap:12px}
.c4k .pill{display:inline-block;padding:3px 10px;border-radius:999px;font-size:11px;
font-weight:500;background:rgba(255,255,255,.04);border:1px solid var(--border);
color:var(--label);white-space:nowrap}
.c4k .pill.pink{background:rgba(236,72,153,.12);border-color:rgba(236,72,153,.35);
color:var(--pink-hi)}
.c4k .pill.blue{background:rgba(83,189,235,.10);border-color:rgba(83,189,235,.30);
color:var(--blue)}
.c4k hr{border:none;border-top:1px solid var(--border);margin:14px 0}
</style>
<div class="c4k">…content…</div>"""

_EXAMPLES = """\
EXAMPLES — five canonical card bodies (prepend the TEMPLATE style block to each):

weather:
<div class="c4k"><div class="row"><div><div class="k">Tel Aviv · now</div>
<div style="font-size:42px;font-weight:700;color:var(--text);margin-top:4px">27°<span
 style="color:var(--muted);font-size:18px">C</span></div>
<div style="color:var(--label)">☀️ Clear · feels like 29°</div></div>
<div style="text-align:right;color:var(--label);font-size:12px"><div>H <span
 style="color:var(--body)">31°</span> · L <span style="color:var(--body)">22°</span></div>
<div>💨 14 km/h</div><div>💧 48%</div></div></div><hr>
<div style="display:grid;grid-template-columns:repeat(5,1fr);text-align:center;gap:4px">
<div><div class="k">15:00</div><div style="font-size:18px;margin:4px 0">☀️</div>
<div style="color:var(--text)">28°</div></div>
<div><div class="k">16:00</div><div style="font-size:18px;margin:4px 0">🌤</div>
<div style="color:var(--text)">28°</div></div>
<div><div class="k">17:00</div><div style="font-size:18px;margin:4px 0">🌤</div>
<div style="color:var(--text)">26°</div></div>
<div><div class="k">18:00</div><div style="font-size:18px;margin:4px 0">🌥</div>
<div style="color:var(--text)">25°</div></div>
<div><div class="k">19:00</div><div style="font-size:18px;margin:4px 0">🌙</div>
<div style="color:var(--blue)">23°</div></div></div></div>

knowledge panel / entity:
<div class="c4k"><div style="display:flex;gap:14px;align-items:center;margin-bottom:12px">
<div style="width:52px;height:52px;border-radius:12px;
background:linear-gradient(135deg,#EC4899,#53BDEB);display:flex;align-items:center;
justify-content:center;font-size:24px">🧮</div>
<div><div style="color:var(--text);font-weight:700;font-size:15px">Ada Lovelace</div>
<div style="color:var(--label);font-size:11px">Mathematician · 1815–1852</div></div></div>
<div style="color:var(--body);font-size:12px;line-height:1.7;margin-bottom:12px">English
 mathematician known for her work on Babbage's Analytical Engine — widely regarded as
 the <span style="color:var(--pink-hi)">first computer programmer</span>.</div>
<div style="display:flex;flex-direction:column;gap:6px;font-size:12px">
<div class="row"><span class="k">born</span><span style="color:var(--body)">Dec 10,
 1815 · London</span></div>
<div class="row"><span class="k">known for</span><span style="color:var(--body)">Note G
 — first algorithm</span></div>
<div class="row"><span class="k">parent</span><span
 style="color:var(--blue)">Lord Byron</span></div></div></div>

flight status:
<div class="c4k"><div class="row" style="margin-bottom:14px"><span class="pill blue">LY
 073 · on time</span><span style="color:var(--muted);font-size:11px">Boeing 787-9</span>
</div><div class="row" style="align-items:flex-end">
<div><div style="font-size:28px;font-weight:700;color:var(--text)">TLV</div>
<div class="k">Tel Aviv</div><div style="color:var(--blue);margin-top:4px">22:40</div></div>
<svg viewBox="0 0 120 24" style="flex:1;height:24px;margin:0 8px 18px">
<line x1="4" y1="12" x2="116" y2="12" stroke="rgba(255,255,255,.14)" stroke-dasharray="3 4"/>
<circle cx="4" cy="12" r="2.5" fill="#EC4899"/><circle cx="116" cy="12" r="2.5" fill="#53BDEB"/>
<text x="54" y="9" font-size="10">✈️</text></svg>
<div style="text-align:right"><div style="font-size:28px;font-weight:700;color:var(--text)">JFK
</div><div class="k">New York</div><div style="color:var(--blue);margin-top:4px">04:05<span
 style="color:var(--muted)">+1</span></div></div></div><hr>
<div style="display:grid;grid-template-columns:repeat(4,1fr);text-align:center">
<div><div class="k">gate</div><div style="color:var(--text);font-weight:600">C8</div></div>
<div><div class="k">seat</div><div style="color:var(--pink-hi);font-weight:600">14A</div></div>
<div><div class="k">board</div><div style="color:var(--text);font-weight:600">21:55</div></div>
<div><div class="k">durat.</div><div style="color:var(--text);font-weight:600">12:25</div></div>
</div></div>

calendar / day agenda (current event gets the pink left border):
<div class="c4k"><div class="row" style="margin-bottom:14px">
<div style="color:var(--text);font-weight:600">Wed, Jun 10</div>
<span class="pill">3 events</span></div>
<div style="display:flex;flex-direction:column;gap:12px">
<div style="display:flex;gap:14px"><div style="color:var(--blue);min-width:52px">09:30</div>
<div style="border-left:2px solid var(--pink);padding-left:12px">
<div style="color:var(--text)">Standup — backend</div>
<div style="color:var(--muted);font-size:11px">15 min · Meet</div></div></div>
<div style="display:flex;gap:14px"><div style="color:var(--blue);min-width:52px">14:00</div>
<div style="border-left:2px solid var(--border-hi);padding-left:12px">
<div style="color:var(--text)">Focus block — media wiring</div>
<div style="color:var(--muted);font-size:11px">2 h · no meetings</div></div></div>
<div style="display:flex;gap:14px"><div style="color:var(--blue);min-width:52px">19:30</div>
<div style="border-left:2px solid var(--border-hi);padding-left:12px">
<div style="color:var(--text)">🏋️ Gym</div>
<div style="color:var(--muted);font-size:11px">1 h</div></div></div></div></div>

to-do list:
<div class="c4k"><div class="row" style="margin-bottom:12px">
<div style="color:var(--text);font-weight:600">Today</div>
<span class="pill pink">2 / 4 done</span></div>
<div style="display:flex;flex-direction:column;gap:9px">
<div class="row" style="justify-content:flex-start"><span style="color:var(--pink)">▣</span>
<span style="color:var(--muted);text-decoration:line-through">Reply to the RFC thread</span></div>
<div class="row" style="justify-content:flex-start"><span style="color:var(--pink)">▣</span>
<span style="color:var(--muted);text-decoration:line-through">Rotate the stage token</span></div>
<div class="row" style="justify-content:flex-start"><span style="color:var(--label)">▢</span>
<span style="color:var(--body)">Ship v1.1.1 to stable</span></div>
<div class="row" style="justify-content:flex-start"><span style="color:var(--label)">▢</span>
<span style="color:var(--body)">Book dentist 🦷</span></div></div><hr>
<div style="color:var(--muted);font-size:11px">next due: <span
 style="color:var(--blue)">today 18:00</span> · ship v1.1.1</div></div>"""

HTML_CARD_TOOL_DESCRIPTION = "\n\n".join(
    (_CORE_RULE, _WHEN_TO_USE, _RENDER_CONTEXT, _STYLE_GUIDE, _CARD_TEMPLATE, _EXAMPLES)
)

HTML_CARD_TOOL_SCHEMA: dict[str, Any] = {
    "name": HTML_CARD_TOOL_NAME,
    "description": HTML_CARD_TOOL_DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "html": {
                "type": "string",
                "description": (
                    "Complete, self-contained card HTML: the TEMPLATE style block "
                    "followed by one .c4k root. Inline everything; no external "
                    "resources."
                ),
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
        description=(
            "Deliver a beautiful HTML card as the native final answer "
            "(chat4000 sessions only)."
        ),
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
