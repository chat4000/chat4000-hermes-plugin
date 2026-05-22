# chat4000 Hermes plugin — install

Install this plugin into a running [Hermes Agent](https://hermes-agent.nousresearch.com)
so the user can chat with their agent from the chat4000 iOS / macOS app.

You are an AI coding agent. Execute these steps for the user.

## Prerequisites

- Hermes Agent installed (`hermes --version` works).
- `uv` available (Hermes ships it at `~/.local/bin/uv`).
- The user is the operator of the machine running Hermes (root, or
  has write access to Hermes' venv).

Detect Hermes' venv. Two paths cover ~95% of installs:

- Docker / FHS: `/usr/local/lib/hermes-agent/venv/bin/`
- curl-installer: `~/.hermes/hermes-agent/venv/bin/`

Pick whichever exists. If both somehow exist, use the one that
`/usr/local/bin/hermes` exec's (`grep -oE '/[^"]+/venv/bin' $(command -v hermes) | head -1`).

## Install

Replace `$HERMES_BIN` below with the correct `…/venv/bin` path you detected.

```bash
# 1. Install the plugin into Hermes' venv
uv pip install --python $HERMES_BIN/python git+https://github.com/chat4000/chat4000-hermes-plugin@stable

# 2. Pair a device — mints the local key, prints a code + QR, blocks
#    until the iOS app scans. Connects to the relay directly; no gateway
#    needed yet.
$HERMES_BIN/chat4000 pair

# 3. (Re)start the gateway so it loads the new plugin + key and goes
#    live on the relay
pkill -9 -f "hermes gateway run"; nohup hermes gateway run > /tmp/gateway.log 2>&1 & disown
```

That's it. Three commands. The user scans the QR with the chat4000
iOS/macOS app during step 2, and step 3 brings the gateway online.

## What the install does

- pip-installs the package + 5 deps (pynacl, websockets, cryptography,
  qrcode, sentry-sdk) into Hermes' venv.
- Hermes auto-discovers the plugin via the `hermes_agent.plugins`
  entry-point group — no manual enable needed.
- `chat4000 pair` mints the 32-byte group key at
  `~/.hermes/plugins/chat4000/keys/default.json` (mode 0600) if missing,
  then opens a direct WebSocket to the relay's pairing room. Idempotent
  on re-runs — an existing key is loaded, not regenerated. No gateway
  process required for this step.
- The gateway restart loads the chat4000 platform. The adapter reads
  the already-minted key from disk and opens a persistent WebSocket to
  `wss://relay.chat4000.com/ws`. From this point on, messages from the
  iOS app flow through to the Hermes agent.

## Verify

```bash
hermes gateway status                  # should show "running"
ls ~/.hermes/plugins/chat4000/keys/    # should contain default.json
```

Send a message from the chat4000 iOS app. The bot should reply within
~3-5 seconds (typing indicator visible immediately).

## Reset

Wipe local state and start over (group key + ack store):

```bash
$HERMES_BIN/chat4000 reset
```

Paired devices keep their old group key and will fail to decrypt until
they re-pair.

## Troubleshooting

- **"plugin chat4000 is registered but adapter creation failed"** —
  no group key. Run `chat4000 setup --no-pair` then restart gateway.
- **iOS app stuck waiting for join** — wrong code. Codes are 8 chars
  from `ABCDEFGHJKMNPRTUVWXYZ2346789` (no 0/1/5/I/L/O/S).
- **No typing indicator** — make sure the plugin version is ≥`b876ccb`
  (the send_typing signature fix). `uv pip install --upgrade --reinstall ...`
- **Gateway doesn't see the plugin** — discovery runs at startup only.
  Restart with `pkill -9 -f "hermes gateway run"; nohup hermes gateway run > /tmp/gateway.log 2>&1 & disown`.

## Source

GitHub: https://github.com/chat4000/chat4000-hermes-plugin
License: GPL-3.0
