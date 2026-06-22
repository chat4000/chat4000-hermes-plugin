"""Two-cursor sliding-sync (protocol D): the to-device cursor is tracked,
persisted, acked, carried forward, and resumed separately from the room `pos`.

GatewayClient is socket-coupled, so we build a bare instance via __new__ and
capture the frames it would send.
"""

from __future__ import annotations

import pytest

from chat4000_hermes_plugin.matrix.cursor_store import CursorStore
from chat4000_hermes_plugin.matrix.gateway_client import (
    GatewayClient,
    GatewayCredentials,
)


def _gw(pos=None, td=None):
    gw = GatewayClient.__new__(GatewayClient)
    gw._last_persisted_pos = pos
    gw._last_persisted_to_device_pos = td
    sent: list = []

    async def _cap(frame):
        sent.append(frame)

    gw._send = _cap  # type: ignore[method-assign,assignment]
    return gw, sent


async def test_ack_sync_echoes_acked_frame_to_device_pos_exactly():
    """ECHO-EXACT (protocol D.1): a sync_ack's to_device_pos echoes EXACTLY the
    acked frame's to_device_pos — present iff that frame carried a to-device
    section, omitted otherwise — and NEVER a carried-forward earlier value."""
    gw, sent = _gw()
    # A frame WITH a to-device cursor: ack both.
    await gw.ack_sync("p1", "td1")
    assert sent[-1] == {"t": "sync_ack", "pos": "p1", "to_device_pos": "td1"}
    # A frame with NO to-device section: OMIT to_device_pos. Do NOT carry td1
    # forward into the ack — the gateway validates the echo against the pending
    # frame and closes with bad_sync_ack on a mismatch. (Carry-forward of the
    # durable cursor lives only in sync_start resume, asserted below.)
    await gw.ack_sync("p2")
    assert sent[-1] == {"t": "sync_ack", "pos": "p2"}
    # But the durable cursor IS still carried forward for reconnect resume.
    assert gw._last_persisted_to_device_pos == "td1"


async def test_ack_sync_omits_to_device_pos_until_first_seen():
    gw, sent = _gw()
    await gw.ack_sync("p1")  # never received a to_device_pos yet
    assert sent[-1] == {"t": "sync_ack", "pos": "p1"}  # absent → gateway unchanged


async def test_sync_start_resends_both_cursors_on_reconnect():
    gw, sent = _gw(pos="p9", td="td9")
    await gw.start_sync({"lists": {}})
    assert sent[-1] == {
        "t": "sync_start",
        "body": {"lists": {}},
        "pos": "p9",
        "to_device_pos": "td9",
    }


async def test_sync_start_omits_cursors_on_fresh_sync():
    gw, sent = _gw()  # both None
    await gw.start_sync({"lists": {}})
    assert sent[-1] == {"t": "sync_start", "body": {"lists": {}}}


def _creds():
    return GatewayCredentials(
        gateway_url="wss://gw/ws",
        access_token="syt",  # noqa: S106  # test fixture token
        app_id="@chat4000/hermes-plugin",
        client_version="1.0.0",
    )


async def _noop_sync(frame):  # GatewayClient requires an on_sync handler
    return None


async def test_request_times_out_when_gateway_never_auths():
    gw = GatewayClient(_creds(), on_sync=_noop_sync, request_timeout=0.01)
    with pytest.raises(TimeoutError):
        await gw.request("GET", "/_matrix/client/v3/account/whoami")
    assert gw._pending == {}


async def test_cursors_survive_a_process_restart(tmp_path, monkeypatch):
    """Protocol D / E "Refresh the new device's keys on redeem": a plugin MUST
    persist pos/to_device_pos durably and REPLAY them on every start so a PROCESS
    restart (a brand-new GatewayClient, simulating a new process) resumes an
    INCREMENTAL sync — a fresh, cursor-less sync would drop the device_lists delta.

    The in-memory carry-forward of the DURABLE cursor (for sync_start resume)
    already covered same-process reconnects; this asserts the durable-storage path
    across a real construction boundary. NB: per ECHO-EXACT (D.1) the carry-forward
    no longer appears in the ack FRAME — only in the persisted cursor + sync_start.
    """
    monkeypatch.setenv("HERMES_STATE_DIR", str(tmp_path))

    # ── Process 1: a real GatewayClient acks a couple of frames. ──
    gw1 = GatewayClient(_creds(), on_sync=_noop_sync, cursor_store=CursorStore("acct1"))
    sent1: list = []

    async def _cap1(frame):
        sent1.append(frame)

    gw1._send = _cap1  # type: ignore[method-assign,assignment]
    await gw1.ack_sync("r1", "t1")
    await gw1.ack_sync("r2")  # no to-device section → ack OMITS to_device_pos (echo-exact)
    assert sent1[-1] == {"t": "sync_ack", "pos": "r2"}
    # The durable to-device cursor is still carried forward in memory + persisted.
    assert gw1._last_persisted_to_device_pos == "t1"

    # ── Process 2: a FRESH GatewayClient (new instance == new process). Its
    #    __init__ loads the persisted cursors; its first sync_start replays both. ──
    gw2 = GatewayClient(_creds(), on_sync=_noop_sync, cursor_store=CursorStore("acct1"))
    sent2: list = []

    async def _cap2(frame):
        sent2.append(frame)

    gw2._send = _cap2  # type: ignore[method-assign,assignment]
    await gw2.start_sync({"lists": {}})
    start = sent2[-1]
    assert start["t"] == "sync_start"
    # BOTH cursors replayed — the latest acked room pos and carried-forward td pos.
    assert start["pos"] == "r2"
    assert start["to_device_pos"] == "t1"


async def test_cursor_store_writes_both_atomically_as_one_object(tmp_path, monkeypatch):
    """The file holds both cursors in one JSON object, written atomically."""
    monkeypatch.setenv("HERMES_STATE_DIR", str(tmp_path))
    store = CursorStore("acct2")
    store.persist("rX", "tX")
    loaded = CursorStore("acct2").load()
    assert loaded.pos == "rX"
    assert loaded.to_device_pos == "tX"
    # A fresh account with no file → cursor-less (fresh) sync, not a crash.
    assert CursorStore("never-written").load().pos is None


def test_cursor_store_clear_pos_keeps_to_device(tmp_path, monkeypatch):
    """Protocol D.1/D.2 `pos_expired`: clearing `pos` discards the room cursor only
    and KEEPS `to_device_pos` (the separate durable token), so a later reconnect
    can't replay the expired pos but the to-device stream stays intact."""
    monkeypatch.setenv("HERMES_STATE_DIR", str(tmp_path))
    store = CursorStore("reset1")
    store.persist("rX", "tX")
    store.clear_cursors(["pos"])
    loaded = CursorStore("reset1").load()
    assert loaded.pos is None
    assert loaded.to_device_pos == "tX"


def test_cursor_store_clear_only_named_cursor(tmp_path, monkeypatch):
    """Only the named cursor is discarded; an unnamed/unknown name is a no-op for the
    survivors."""
    monkeypatch.setenv("HERMES_STATE_DIR", str(tmp_path))
    store = CursorStore("reset2")
    store.persist("rX", "tX")
    # Clearing only to_device_pos leaves pos.
    store.clear_cursors(["to_device_pos"])
    loaded = CursorStore("reset2").load()
    assert loaded.pos == "rX"
    assert loaded.to_device_pos is None
    # An unknown cursor name clears nothing.
    store.clear_cursors(["bogus"])
    again = CursorStore("reset2").load()
    assert again.pos == "rX"


def test_cursor_store_clear_on_missing_file_is_safe(tmp_path, monkeypatch):
    """Clearing when no file exists must not crash and leaves nothing to replay."""
    monkeypatch.setenv("HERMES_STATE_DIR", str(tmp_path))
    store = CursorStore("reset3")
    store.clear_cursors(["pos"])  # no file yet
    assert CursorStore("reset3").load().pos is None


def test_sync_reset_pos_expired_clears_room_pos_keeps_to_device(tmp_path, monkeypatch):
    """A `sync_reset {reason: pos_expired, cursors: [pos]}` frame discards the room
    cursor (in memory AND durably) and KEEPS the to-device cursor — and sends NO
    new sync_start (the gateway already re-initialised the upstream on this socket)."""
    monkeypatch.setenv("HERMES_STATE_DIR", str(tmp_path))
    store = CursorStore("gwreset")
    store.persist("r5", "t5")
    gw = GatewayClient(_creds(), on_sync=_noop_sync, cursor_store=store)
    # __init__ loaded both cursors from the store.
    assert gw._last_persisted_pos == "r5"
    assert gw._last_persisted_to_device_pos == "t5"
    sent: list = []

    async def _cap(frame):
        sent.append(frame)

    gw._send = _cap  # type: ignore[method-assign,assignment]
    gw._handle_sync_reset({"t": "sync_reset", "reason": "pos_expired", "cursors": ["pos"]})

    # In-memory: room pos gone, to-device kept.
    assert gw._last_persisted_pos is None
    assert gw._last_persisted_to_device_pos == "t5"
    # Durable: same.
    loaded = CursorStore("gwreset").load()
    assert loaded.pos is None
    assert loaded.to_device_pos == "t5"
    # The device does NOT send a new sync_start in response to sync_reset.
    assert sent == []


def test_sync_reset_with_no_cursors_is_a_noop(tmp_path, monkeypatch):
    """A reset that names no cursors clears nothing (defensive — never guess)."""
    monkeypatch.setenv("HERMES_STATE_DIR", str(tmp_path))
    store = CursorStore("gwreset2")
    store.persist("r6", "t6")
    gw = GatewayClient(_creds(), on_sync=_noop_sync, cursor_store=store)
    gw._handle_sync_reset({"t": "sync_reset", "reason": "pos_expired"})  # cursors absent
    assert gw._last_persisted_pos == "r6"
    assert gw._last_persisted_to_device_pos == "t6"
    assert CursorStore("gwreset2").load().pos == "r6"


def test_sync_reset_malformed_cursors_does_not_crash(tmp_path, monkeypatch):
    """A non-list `cursors` is logged and ignored — a bad frame must not crash the
    read loop or clear anything."""
    monkeypatch.setenv("HERMES_STATE_DIR", str(tmp_path))
    store = CursorStore("gwreset3")
    store.persist("r7", "t7")
    gw = GatewayClient(_creds(), on_sync=_noop_sync, cursor_store=store)
    gw._handle_sync_reset({"t": "sync_reset", "reason": "pos_expired", "cursors": "pos"})
    assert gw._last_persisted_pos == "r7"
    assert CursorStore("gwreset3").load().pos == "r7"


async def test_sync_reset_dispatch_routes_to_handler(tmp_path, monkeypatch):
    """The frame dispatcher routes a `sync_reset` frame to the cursor-reset handler
    (not the sync queue), exercising the parse path end to end."""
    monkeypatch.setenv("HERMES_STATE_DIR", str(tmp_path))
    store = CursorStore("gwreset4")
    store.persist("r8", "t8")
    gw = GatewayClient(_creds(), on_sync=_noop_sync, cursor_store=store)
    await gw._dispatch('{"t":"sync_reset","reason":"pos_expired","cursors":["pos"]}')
    assert gw._last_persisted_pos is None
    assert gw._last_persisted_to_device_pos == "t8"
    # The reset frame must NOT have been enqueued for the sync worker.
    assert gw._sync_queue.empty()
