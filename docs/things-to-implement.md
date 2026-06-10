# Things To Implement

Forward backlog of known-but-unbuilt work for the chat4000 Hermes plugin.
Distinct from `patches-to-remember.md` (workarounds we carry) and `MIGRATION.md`
(v1→v2 migration + backend pushbacks).

Each item: **where**, **why**, **status**. Keep entries accurate — delete when
shipped, don't let them rot. Add a `[done: <sha>]` tag instead of silently
removing if the history matters.

---

## Media (inbound attachments)

### T1 — Wire `m.video` + `m.file` inbound attachments
- **Where:** `matrix/hermes_adapter.py::_on_user_message`, the media branch.
- **Why:** Only `m.image`→`PHOTO` and `m.audio`→`AUDIO` are handled. `m.video`
  and `m.file` fall through with `message_type` left as `TEXT` and **no
  `media_urls` appended** — so a video or document is silently dropped (no error,
  nothing reaches Hermes). The host enum already has `VIDEO` and `DOCUMENT`
  (`gateway/platforms/base.py::MessageType`). Mirror the image/audio paths:
  download → decrypt → cache → append path → set the right `MessageType`.
- **Status:** OPEN. Same bug class as the image `IMAGE`→`PHOTO` fix (`0b2af78`),
  just not yet user-reported.

### T2 — Stop the media `except` from masking code bugs
- **Where:** `matrix/hermes_adapter.py::_on_user_message`, the `try/except` around
  the media download/decrypt block.
- **Why:** The broad `except Exception` swallowed a real programming error
  (`MessageType.IMAGE` — a symbol that never existed) and logged it as "inbound
  media decrypt failed: IMAGE". A *code* bug was disguised as a *runtime media*
  failure, so it survived to production instead of failing a test or crashing
  loudly. Separate expected failures (decrypt/hash mismatch, download/network)
  from programming errors (`AttributeError`/`NameError`/`KeyError` on our own
  code) — let the latter surface as bugs.
- **Status:** OPEN. This is *why* T1's sibling bug reached the user.

### T3 — Reconsider the 60s media download timeout
- **Where:** `matrix/media.py::MediaClient.timeout` (= 60.0).
- **Why:** A slow/unreachable media host stalls a single attachment up to 60s
  before it fails. Lowering to ~10–15s fails fast, but risks cutting off a
  genuinely slow-but-valid download — a tradeoff, not a clear win. Decide with
  evidence from a real slow-download trace.
- **Status:** OPEN (optional). Deferred when O2 shipped (`3b556f8`); explicitly
  not bundled.

---

## Sessions

### T4 — Session naming (auto-title → `m.room.name`)
- **Where:** `plugin_hooks.py::on_post_llm_call` + `_poll_host_title`, applied
  via `matrix/hermes_adapter.py::_apply_host_session_title` →
  `matrix/rooms.py::maybe_apply_host_title` (refuses to clobber manual renames).
- **How (chosen):** the host never delivers the auto-title to platform adapters
  (Telegram-only callback), so the plugin hooks `post_llm_call` (first exchanges
  only — mirrors the host's <= 2-user-messages heuristic) and polls
  `hermes_state.SessionDB(read_only=True).get_session_title()` every 2s, 60s
  budget, on the adapter loop. One poller per session; titled rooms are never
  re-polled. An upstream `on_session_title` hook PR is pending — when it lands,
  replace the poll with the push.
- **Status:** DONE (poll-based pickup). T5 is unblocked.

### T5 — Name the auto-created initial session room
- **Where:** `matrix/hermes_adapter.py::_ensure_initial_session`.
- **Why:** The pairing auto-session (`4be87c2`) creates the room with the default
  "session" title. T4 landed, so the auto-titler now names it from the first
  exchange; verify the initial room actually picks the title up.
- **Status:** OPEN, unblocked (T4 done).

---

## Install / readiness

### T6 — Post-pair "✓ ready" timeline message
- **Where:** `matrix/hermes_adapter.py` (post-pair, after the freshly-paired
  user's keys are exchanged) + `install_wizard.py` already covers the W1 marker.
- **Why:** Two waits get conflated. W1 (server-up) is gated by the ready marker.
  W2 (post-pair key exchange: the phone learning the plugin's device + Megolm key
  share) is an unavoidable seconds→~1min wait the plugin can only TOLERATE and
  SIGNAL, not pre-do. Today it's a silent scary gap. Post a one-time "✓ ready"
  message once the new user's keys are exchanged so the wait is visible (the
  Telegram-style honest signal). Keep the install marker (O1); this adds O3.
- **Status:** OPEN (future).

---

## External (other repos — tracked, not fixed here)

### T8 — iOS client must dedupe timeline events by `event_id`
- **Why:** The ×3 duplication of tool bubbles is downstream: the plugin sends
  each event once and its own sync receives each once. The client must dedupe by
  `event_id` (mandated in protocol.md E). Plugin-side nothing to do.
- **Status:** EXTERNAL (iOS / WS gateway).

### T9 — Test-box DNS flakiness
- **Why:** `hermes-test-NN` intermittently fails name resolution for the media
  host (`Temporary failure in name resolution`), which can drop a random
  attachment. Environment/network, not plugin code — do not band-aid in the
  plugin.
- **Status:** EXTERNAL (box/infra).
