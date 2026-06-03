"""Two-cursor sliding-sync (protocol D): the to-device cursor is tracked,
persisted, acked, carried forward, and resumed separately from the room `pos`.

GatewayClient is socket-coupled, so we build a bare instance via __new__ and
capture the frames it would send.
"""

from __future__ import annotations

from chat4000_hermes_plugin.matrix.gateway_client import GatewayClient


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
