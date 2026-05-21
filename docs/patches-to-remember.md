# Patches To Remember

Code we carry that exists ONLY because of an upstream bug or contract
asymmetry. Each entry has a "remove when" condition so we don't keep
the workaround forever after upstream fixes the root cause.

Audit this file on every Hermes version bump.

---

## P1 — `post_llm_call` sweep for orphan tool bubbles

**Where:** `chat4000_hermes_plugin/plugin_hooks.py` → `on_post_llm_call`

**Why:** Hermes' `pre_tool_call` / `post_tool_call` hooks are
asymmetric. Some tools (`todo`, `memory`, `session_search`,
`delegate_task`, `clarify`) are intercepted by the agent loop in
`agent/tool_executor.py:598+` and never reach
`model_tools.py:handle_function_call` — which is the only place that
fires `invoke_hook("post_tool_call", ...)`. So:

- `pre_tool_call` ✓ fires (from `get_pre_tool_call_block_message`)
- tool runs inline in the agent loop
- `post_tool_call` ✗ never fires
- our `_PENDING_TOOL_CALLS[(task_id, tool_name)]` entry never gets
  popped → no `tool_end` frame → iOS bubble spinner spins forever

**The patch:** register `post_llm_call` as a backstop. At end of every
turn, drain `_PENDING_TOOL_CALLS` for the closing session and emit
synthetic `tool_end(status=done, result="")` for every orphan.

**Limitations** (documented because the user asked):

1. Sweep only runs `if final_response and not interrupted` — user
   interrupt or LLM crash leaves orphans forever.
2. `delegate_task` runs minutes-long subagents → bubble appears stuck
   the whole time, then snaps to ✓ only when the outer turn ends.
3. We fabricate `status=done` + empty `result` — UI says ✓ but we
   don't know the real outcome. If a Path-B tool actually failed,
   the iOS bubble would mis-show ✓.
4. Sweep keys by `(task_id, tool_name)` and filters by
   `key[0] == session_id`. Sub-agent / cron orphans with different
   task_ids never get swept → permanent leak in `_PENDING_TOOL_CALLS`.

**Hermes source references** (v0.14.0, commit on `main` as of
2026-05-21):

- `model_tools.py:495` — `_AGENT_LOOP_TOOLS = {"todo", "memory",
  "session_search", "delegate_task"}` — the intercepted list
- `agent/tool_executor.py:598` — `elif function_name == "todo": ...
  _todo_tool(...)` — inline execution path
- `model_tools.py:851` — `invoke_hook("post_tool_call", ...)` — the
  only post-hook fire site, unreachable for the intercepted tools
- `agent/conversation_loop.py:3965` — `invoke_hook("post_llm_call",
  ...)` — our backstop fire site, gated on `final_response and not
  interrupted`

**Remove when:** Hermes patches `handle_function_call` or the inline
agent-loop handlers to fire `post_tool_call` consistently. Either of:

- An `invoke_hook("post_tool_call", ...)` call lands inside each of
  the `elif function_name == "todo"` / `memory` / `session_search` /
  `delegate_task` branches in `agent/tool_executor.py`
- OR `handle_function_call` is refactored so the `_AGENT_LOOP_TOOLS`
  branch fires post_tool_call before its early return

**Upstream issue:** TODO file at NousResearch/hermes-agent — should
link the issue # here once filed.

**How to remove safely:**

1. Delete `on_post_llm_call` from `plugin_hooks.py` (and its
   `ctx.register_hook` line).
2. Delete this entry from this file.
3. Run tests — no test should depend on the sweep (it's a fallback,
   not part of the documented contract).
4. Manually verify `todo` bubble closes ✓ without the sweep, on the
   target Hermes version.
