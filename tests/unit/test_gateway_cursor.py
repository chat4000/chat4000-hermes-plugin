"""Two-cursor sliding-sync (protocol D): the to-device cursor is tracked,
persisted, acked, carried forward, and resumed separately from the room `pos`.

GatewayClient is socket-coupled, so we build a bare instance via __new__ and
capture the frames it would send.
"""

from __future__ import annotations

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


async def test_ack_sync_sends_both_and_carries_forward():
    gw, sent = _gw()
    # A frame WITH a to-device cursor: ack both.
    await gw.ack_sync("p1", "td1")
    assert sent[-1] == {"t": "sync_ack", "pos": "p1", "to_device_pos": "td1"}
    # A frame with NO to-device section: carry td1 forward (omitting it would tell
    # the gateway to leave the cursor unchanged — but the spec says carry it).
    await gw.ack_sync("p2")
    assert sent[-1] == {"t": "sync_ack", "pos": "p2", "to_device_pos": "td1"}


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


async def test_cursors_survive_a_process_restart(tmp_path, monkeypatch):
    """Protocol D / E "Refresh the new device's keys on redeem": a plugin MUST
    persist pos/to_device_pos durably and REPLAY them on every start so a PROCESS
    restart (a brand-new GatewayClient, simulating a new process) resumes an
    INCREMENTAL sync — a fresh, cursor-less sync would drop the device_lists delta.

    The in-memory-only carry-forward already covered same-process reconnects; this
    asserts the durable-storage path across a real construction boundary.
    """
    monkeypatch.setenv("HERMES_STATE_DIR", str(tmp_path))

    # ── Process 1: a real GatewayClient acks a couple of frames. ──
    gw1 = GatewayClient(_creds(), on_sync=_noop_sync, cursor_store=CursorStore("acct1"))
    sent1: list = []

    async def _cap1(frame):
        sent1.append(frame)

    gw1._send = _cap1  # type: ignore[method-assign,assignment]
    await gw1.ack_sync("r1", "t1")
    await gw1.ack_sync("r2")  # no to-device section → t1 carries forward
    assert sent1[-1] == {"t": "sync_ack", "pos": "r2", "to_device_pos": "t1"}

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
