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
- **Where:** new wiring in `matrix/` + `matrix/commands.py::_session_rename`.
- **Why:** Auto-created session rooms start unnamed ("session"). Hermes already
  LLM-auto-titles a session from the first exchange via a `title_callback` (the
  same hook Telegram uses to rename its forum topic). Mirror it: hook that
  callback → set `m.room.name`; map our `session.rename` command →
  `hermes_state.set_session_title`; add a per-room "disable auto-rename" config.
  Don't invent a naming scheme — reuse the agent's. (Research done; see the
  session-handoff notes.)
- **Status:** OPEN (future feature).

### T5 — Name the auto-created initial session room
- **Where:** `matrix/hermes_adapter.py::_ensure_initial_session`.
- **Why:** The pairing auto-session (`4be87c2`) creates the room with the default
  "session" title. Once T4 lands, the auto-titler will name it from the first
  exchange; until then it stays generic. Tracked so it isn't forgotten.
- **Status:** OPEN, blocked on T4.

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
