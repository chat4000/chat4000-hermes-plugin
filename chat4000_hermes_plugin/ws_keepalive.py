"""WebSocket frame-level keepalive helper.

The `websockets` library handles WS protocol PING/PONG automatically via
its own keepalive (ping_interval/ping_timeout kwargs to connect()), so
this module is much smaller than the TS impl which had to hand-roll it
on the `ws` package.

We expose this anyway because the TS plugin's tests reference it and
keeping symbol parity makes diffing the two implementations easier."""

DEFAULT_KEEPALIVE_SECS = 25.0


def configured_keepalive_kwargs() -> dict:
    """Returned by transport/relay.py for the websockets.connect() call.
    Combines our app-layer ping (every 25 s) with WS-frame keepalive at
    the same cadence so dead sockets are caught at either layer."""
    return {
        "ping_interval": DEFAULT_KEEPALIVE_SECS,
        "ping_timeout": 15.0,
    }
