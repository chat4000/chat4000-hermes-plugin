# v1 → v2 (Matrix) migration

Branch: `v2-matrix-migration`. v2 replaces the custom relay + group-key crypto +
bespoke pairing with **Matrix** (gateway WS + sliding sync + Olm/Megolm E2EE via
the `chat4000-pyvodozemac` binding + registrar OTP pairing). See the protocol at
`../chat4000-backend-depolyment-and-docs/docs/protocol.md` and the cross-repo
plan in `../chat4000-pyvodozemac/PLAN.md`.

## Architecture (O2)

- **`chat4000-pyvodozemac`** (sibling repo, Rust/PyO3) — wraps matrix-sdk-crypto's
  OlmMachine. Zero networking. The plugin depends on its wheel.
- **This plugin** owns the gateway socket, sliding sync, rooms/turns/tools,
  registrar HTTP, and Hermes integration — and calls the binding for crypto.

## Done (P1–P4 core, all unit-tested — 16 new tests, 215 total green)

- `matrix/gateway_client.py` — WS gateway client (`auth/reauth/req/resp/sync/
  sync_ack`, id-correlated requests, reconnect with `pos` resume). Matches the
  real `chat4000-matrix-ws-proxy/src/protocol.rs`.
- `matrix/registrar_client.py` — registrar HTTP: register/redeem/status/version,
  `self_onboard()`, `poll_until_complete()`.
- `matrix/sliding_sync.py` — request builder + `sync`-frame parser. **Tested.**
- `matrix/crypto_driver.py` — drives the pyvodozemac OlmMachine; owns the
  persist→`sync_ack` ordering (anti-UTD) + encrypt/decrypt + cleartext envelope
  splicing. **Tested (incl. the anti-UTD ordering invariant).**
- `matrix/rooms.py` — space + control/session rooms, `chat4000.room_kind`,
  `m.room.encryption`, invites, rename/archive.
- `matrix/turns.py` — turn anchor, `m.replace` streaming, `chat4000.tool` events
  (2 sends/tool), `chat4000.status`, full `chat4000.push` discipline.
- `matrix/creds_store.py` — bot-creds + crypto-store paths (replaces the v1
  group-key file).
- `matrix/session.py` — orchestrator: build stack, sync loop, **command-boundary
  routing** (commands control-room-only). **Tested.**
- `matrix/commands.py` — `session.*` + `plugin.update_check`; `plugin.update`
  refused (X4). **Tested.**
- `matrix/hermes_adapter.py` — capstone: BasePlatformAdapter over MatrixSession;
  inbound → `handle_message`, replies → TurnWriter streaming/tools, typing →
  status. (Hermes-runtime-coupled; not unit-tested offline.)

## Done (entry + pairing wired)

- `adapter.py` — rewritten to the v2 entry: registers
  `matrix.hermes_adapter.Chat4000MatrixAdapter`; v2 `check/validate/env_enablement`
  off bot creds. (v1 relay no longer imported on the entry path.)
- `cli.py` — rewritten: `pair` = self-onboard (first run) + 6-digit OTP register +
  poll + record paired user; `status`/`reset` for v2 state; `/version` boot gate;
  wizard + telemetry kept.
- `matrix/users_store.py` — known-users store; adapter invites + key-shares them
  on connect.

## TODO (remaining / P5–P7)

- `plugin_hooks.py` — if Hermes' standard runner doesn't fire the reply-pipeline
  tool hooks, route `pre/post_tool_call` to the adapter's TurnWriter (tool bubbles).
- Media (P6): download+decrypt inbound `m.image`/`m.audio` over the HTTP media
  path (D.3) for vision/STT; outbound media.
- Delete the v1 modules (below) + their tests; add `chat4000-pyvodozemac` to deps.
- Build the `chat4000-pyvodozemac` wheel (first networked `cargo build`).

## Delete when the rework lands (do NOT delete yet — still imported)

`crypto.py`, `pairing.py`, `pairing_logger.py`, `transport/relay.py`,
`transport/mock.py`, `transport/registry.py`, `transport/__init__.py`,
`recv_ack_batcher.py`, `ack_store.py`, `ws_keepalive.py`, the group-key parts of
`key_store.py`/`accounts.py`, and the v1 wire types in `protocol_types.py`.
Their unit tests go with them.

## Keep (infra, unchanged)

`telemetry.py`, `analytics.py`, `error_log.py`, `log_rotate.py`,
`logging_setup.py`, `runtime_logger.py`, `package_info.py`, `reconnect.py`
(reused by the gateway client).

## Pushbacks to backend (blocking/limiting)

X1 no cross-signing (key-share device-injection), X2 shared service token on user
machines, X3 bot-token rotation destroys identity, X4 `plugin.update` owner model
undefined (feature deferred), X-sync-ack deployed gateway lacks `sync_ack`
(auto-advances cursor → UTD), X5 streaming-edits vs 900 msg/min + storage.
