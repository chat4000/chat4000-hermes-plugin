# chat4000 plugin for Hermes Agent

Connect a [chat4000](https://chat4000.com) iOS / macOS / CLI client to your
[Hermes Agent](https://github.com/NousResearch/hermes-agent) via the
end-to-end-encrypted chat4000 relay. The relay only sees ciphertext.

Same wire protocol, pairing model, and crypto as the original
`@chat4000/openclaw-plugin` — this is the Python port for Hermes.

## What works

- ✅ Pair an iPhone, Mac, or CLI client with the host-side `chat4000 pair` command
- ✅ Send text / images / voice notes between agent and client
- ✅ Streaming agent replies (text_delta / text_end per protocol §6.4.2)
- ✅ Reliable delivery layer (recv_ack / relay_recv_ack per §6.6)
- ✅ Tool-call streaming — when Hermes invokes a tool (bash, web search,
  read_file, etc.), the call shows up live in the chat as an expandable
  bubble with name, args, status, duration, and (optional) streamed
  stdout. Wire types: `tool_start` / `tool_delta` / `tool_end`.

## What's intentionally NOT here (yet)

- Multi-session resume list (Tier 2-D)
- Model picker per message (Tier 2-E)
- File / share-extension attachments beyond images (Tier 2-F)
- Workspace browser, task monitor, skills directory, usage analytics

The relay, Swift app, and CLI client are unchanged by this plugin —
those repos sit above the plugin and don't care which agent
implementation runs underneath.

## Install

For current Hermes builds, install the plugin by GitHub owner/repo, then
pair a client and restart the gateway:

```sh
hermes plugins install chat4000/chat4000-hermes-plugin
chat4000 pair
hermes gateway restart
```

`chat4000 pair` prints a pairing code and QR payload, then waits for the
chat4000 iOS/macOS app or CLI client to join. Keep it running while you
scan the QR code or type the 8-character code into the client. When the
command prints `Status: [5/5] Pairing complete`, restart the gateway so
Hermes loads the new plugin state.

If `chat4000` is not on `PATH`, run it from Hermes' venv, for example
`~/.hermes/hermes-agent/venv/bin/chat4000 pair`.

Do not use `hermes chat4000 pair` on Hermes versions that do not expose
plugin CLI groups as top-level commands.

## Files

| File | Lines | Purpose |
|---|---:|---|
| `chat4000_hermes_plugin/adapter.py` | ~430 | Chat4000Adapter — Hermes BasePlatformAdapter |
| `chat4000_hermes_plugin/transport/relay.py` | ~480 | WebSocket transport + §6.6 ack flow |
| `chat4000_hermes_plugin/transport/__init__.py` | ~80 | MessageTransport ABC |
| `chat4000_hermes_plugin/transport/registry.py` | ~40 | Per-account transport singleton |
| `chat4000_hermes_plugin/transport/mock.py` | ~140 | Test mock |
| `chat4000_hermes_plugin/pairing.py` | ~360 | Joiner + initiator pairing |
| `chat4000_hermes_plugin/crypto.py` | ~190 | XChaCha20-Poly1305 + X25519 wrap |
| `chat4000_hermes_plugin/ack_store.py` | ~170 | SQLite watermark + dedup |
| `chat4000_hermes_plugin/recv_ack_batcher.py` | ~170 | Flow A cumulative ack |
| `chat4000_hermes_plugin/dispatch/stream_dispatcher.py` | ~230 | §6.4.2 text streaming invariants |
| `chat4000_hermes_plugin/dispatch/tool_call_dispatcher.py` | ~210 | Tool-call streaming (NEW) |
| `chat4000_hermes_plugin/key_store.py` | ~220 | Group-key file storage |
| `chat4000_hermes_plugin/accounts.py` | ~130 | Config resolution |
| `chat4000_hermes_plugin/session_binding.py` | ~210 | Hermes session ↔ chat4000 group |
| `chat4000_hermes_plugin/cli.py` | ~360 | `chat4000 *` host-side commands |
| `chat4000_hermes_plugin/protocol_types.py` | ~270 | Wire-type dataclasses |
| `chat4000_hermes_plugin/telemetry.py` | ~210 | Sentry — opt-in, on by default |
| `chat4000_hermes_plugin/runtime_logger.py` | ~80 | Structured runtime log |
| `chat4000_hermes_plugin/pairing_logger.py` | ~130 | Pairing trace log |
| `chat4000_hermes_plugin/error_log.py` | ~70 | Crash trace dump |
| `chat4000_hermes_plugin/log_rotate.py` | ~30 | 10 MB log rotation |
| `chat4000_hermes_plugin/reconnect.py` | ~50 | Exponential backoff |
| `chat4000_hermes_plugin/ws_keepalive.py` | ~20 | WS-frame keepalive kwargs |
| `chat4000_hermes_plugin/package_info.py` | ~25 | Version from pyproject.toml |

## Tool calls

Three new inner-message types ride inside the existing encrypted envelope:

```text
tool_start  { tool_id, name, args }                — once per call
tool_delta  { tool_id, delta }                      — optional streaming output
tool_end    { tool_id, status, result, duration_ms } — once per call
```

`tool_id` is the stable correlator (analog of `body.stream_id` for text).
Each wire frame gets a fresh `inner.id` per protocol §6.4.2. Receivers
dedupe by `inner.id` per §6.6.9 and merge by `tool_id`.

Protocol version stays at `1` — these are additive types that older
receivers silently ignore.

## Security

- 32-byte group key = the only durable secret
- Stored at `~/.hermes/plugins/chat4000/keys/<account>.json` (chmod 0600)
- Relay sees ciphertext only — XChaCha20-Poly1305 with the group key
- Pairing uses X25519 ECDH + sha256-derived wrap key for one-shot
  group-key transfer (the relay never sees the wrapping key)
- No plaintext logging, ever (even at debug level)

## Telemetry

Anonymous Sentry crash reports, on by default. Opt out three ways:

```sh
chat4000 telemetry disable
export CHAT4000_TELEMETRY_DISABLED=1
chat4000 --no-telemetry <command>
```

## License

GPL-3.0-or-later. Copyright © 2026 NeonNode Ltd.
