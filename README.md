# chat4000 plugin for Hermes Agent (v1.1 — Matrix)

Connect a [chat4000](https://chat4000.com) iOS / macOS / CLI client to your
[Hermes Agent](https://github.com/NousResearch/hermes-agent) over **Matrix**
(chat4000 protocol v2). End-to-end encrypted with Matrix-native Olm/Megolm — the
homeserver and gateway see ciphertext only.

> **v1.1 is a backend rewrite.** The custom relay + 32-byte group key + bespoke
> pairing of v1.0 are gone, replaced by Matrix (Tuwunel homeserver behind a WS
> gateway, a registrar for OTP pairing, Olm/Megolm E2EE). It does **not**
> interoperate with the v1 relay. See [`MIGRATION.md`](MIGRATION.md).

## Architecture

The plugin owns the Matrix client (gateway WebSocket, sliding sync, rooms,
turns) and calls a small Rust binding for the crypto:

- **[`chat4000-pyvodozemac`](../chat4000-pyvodozemac)** (sibling repo) — a
  PyO3/maturin wheel wrapping `matrix-sdk-crypto`'s `OlmMachine`. The only
  production Matrix E2EE stack is Rust (libolm is deprecated); this binding is
  how Python uses it. Does zero networking — the plugin drives it.
- **this plugin** — `chat4000_hermes_plugin/matrix/`: gateway client, sliding
  sync, the crypto driver, rooms/turns, registrar HTTP, the Hermes adapter.

## What works

- ✅ Self-onboard a bot identity + pair a device with a **6-digit OTP**
  (`chat4000 pair`)
- ✅ Send text / images / voice notes (encrypted Matrix attachments) between
  agent and client
- ✅ Streaming agent replies as a self-updating message (`m.replace` edits)
- ✅ Tool-call bubbles (`chat4000.tool` events) + live status (`chat4000.status`)
- ✅ Per-event push control (`chat4000.push`) — only the finished answer wakes you
- ✅ Control-room session commands (`session.new` / `rename` / `archive`)
- ✅ Control-room plugin self-update (`plugin.update_check` / `plugin.update`)
- ✅ Anti-UTD sync discipline (persist room keys before `sync_ack`)

## Install

```sh
# 1. build + install the crypto binding (needs Rust + network)
( cd ../chat4000-pyvodozemac && maturin build --release )
hermes plugin install chat4000-hermes-plugin   # + the built wheel
# 2. onboard + pair (needs CHAT4000_SERVICE_TOKEN)
hermes chat4000 pair
# 3. (re)start the gateway so it loads the plugin + invites the paired user
hermes gateway restart
```

## Key files

| File | Purpose |
|---|---|
| `matrix/gateway_client.py` | WS gateway: auth/req/resp/sync/sync_ack, reconnect |
| `matrix/sliding_sync.py` | sliding-sync request + frame parser |
| `matrix/crypto_driver.py` | drives the OlmMachine binding; anti-UTD ordering |
| `matrix/rooms.py` · `turns.py` | space/rooms; turn anchor, edits, tools, status |
| `matrix/registrar_client.py` | registrar HTTP (OTP pairing + version) |
| `matrix/session.py` · `commands.py` | orchestrator + control-room commands |
| `matrix/media.py` | encrypted attachments over the HTTP media path |
| `matrix/hermes_adapter.py` | BasePlatformAdapter ↔ MatrixSession |
| `adapter.py` · `cli.py` · `plugin_hooks.py` | entry, CLI, tool hooks |

## Security

- The durable secret is the Matrix bot **device token** + the Olm/Megolm crypto
  store (SQLite, owned by the binding). Stored under
  `~/.hermes/plugins/chat4000/` (mode 0600).
- All rooms (incl. the control room) are E2E-encrypted; only `chat4000.push` and
  `m.relates_to` ride cleartext on the envelope, by design.
- No plaintext logging, ever.

## Known limitations / pushbacks (see MIGRATION.md)

- Device verification / cross-signing not yet implemented (TOFU key sharing).
- `plugin.update` runs the registrar-selected install script, then restarts Hermes
  when requested.
- Needs the deployed gateway to implement `sync_ack` (anti-UTD).

## Telemetry

Anonymous Sentry + PostHog, on by default. Opt out: `chat4000 telemetry disable`,
`CHAT4000_TELEMETRY_DISABLED=1`, or `--no-telemetry`.

## License

GPL-3.0-or-later. Copyright © 2026 NeonNode Ltd.
