"""Completion listener (protocol C.4) — the resident plugin completes pairings
without any CLI watcher: it polls every outstanding code in the durable store,
records redeems as they land, settles single-use codes, keeps reusable codes
alive until expiry, and skips codes claimed by an in-process watcher.
"""

from __future__ import annotations

from chat4000_hermes_plugin.matrix.pair_codes_store import (
    PendingCode,
    add_pending_code,
    load_pending_codes,
)
from chat4000_hermes_plugin.matrix.pair_listener import CompletionListener
from chat4000_hermes_plugin.matrix.registrar_client import RegistrarError

ACCOUNT = "default"


class FakeRegistrar:
    """Replays scripted /pair/status payloads per code (last repeats forever)."""

    def __init__(self, scripts: dict[str, list]):
        self.scripts = {c: list(s) for c, s in scripts.items()}
        self.calls: list[str] = []

    async def status(self, code):
        self.calls.append(code)
        script = self.scripts[code]
        out = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(out, Exception):
            raise out
        return out


class Recorder:
    def __init__(self):
        self.redeems: list[tuple[str, dict]] = []
        self.transitions: list[tuple[str, str]] = []

    async def on_redeem(self, record, status, entry):
        self.redeems.append((record.code, dict(entry)))

    async def on_transition(self, record, state, status):
        self.transitions.append((record.code, state))


def _listener(reg, rec) -> CompletionListener:
    return CompletionListener(
        account_id=ACCOUNT,
        registrar=reg,
        on_redeem=rec.on_redeem,
        on_transition=rec.on_transition,
    )


def _pending(code, *, reusable=False, pair_id=None, registered_at_ms=0):
    return PendingCode(
        code=code,
        expires_at_ms=9999999999999,
        reusable=reusable,
        registered_at_ms=registered_at_ms,
        pair_id=pair_id,
    )


async def test_listener_completes_a_pairing_without_the_cli_watcher():
    """The headline case: a code registered by a long-gone CLI process is in the
    store; the resident listener alone observes the redeem and settles it."""
    add_pending_code(_pending("111111"), ACCOUNT)
    reg = FakeRegistrar(
        {
            "111111": [
                {"status": "pending", "redeems": [], "redeemed_count": 0},
                {
                    "status": "completed",
                    "user_id": "@u:hs",
                    "redeems": [{"device_id": "D1", "redeemed_at": 1}],
                    "redeemed_count": 1,
                },
            ]
        }
    )
    rec = Recorder()
    listener = _listener(reg, rec)

    await listener._scan_once()  # pending — nothing yet
    assert rec.redeems == []
    listener._next_poll_at.clear()  # due immediately for the test
    await listener._scan_once()  # completed — redeem recorded, code settled

    assert rec.redeems == [("111111", {"device_id": "D1", "redeemed_at": 1})]
    assert rec.transitions == [("111111", "completed")]
    assert load_pending_codes(ACCOUNT) == []  # settled codes leave the store


async def test_reusable_code_stays_pending_and_records_each_new_redeem():
    """C.3: a reusable code never settles to completed — each redeem adds a
    device; the listener fires on_redeem once per NEW redeem and keeps watching."""
    add_pending_code(_pending("222222", reusable=True), ACCOUNT)
    reg = FakeRegistrar(
        {
            "222222": [
                {
                    "status": "pending",
                    "user_id": "@u:hs",
                    "redeems": [{"device_id": "D1", "redeemed_at": 1}],
                    "redeemed_count": 1,
                },
                {
                    "status": "pending",
                    "user_id": "@u:hs",
                    "redeems": [
                        {"device_id": "D1", "redeemed_at": 1},
                        {"device_id": "D2", "client_id": "cid-2", "redeemed_at": 2},
                    ],
                    "redeemed_count": 2,
                },
            ]
        }
    )
    rec = Recorder()
    listener = _listener(reg, rec)

    await listener._scan_once()
    listener._next_poll_at.clear()
    await listener._scan_once()
    listener._next_poll_at.clear()
    await listener._scan_once()  # same payload again — no NEW redeem, no re-fire

    assert [e["device_id"] for _, e in rec.redeems] == ["D1", "D2"]
    assert rec.transitions == []  # never settles while live
    records = load_pending_codes(ACCOUNT)
    assert len(records) == 1 and records[0].redeemed_count_seen == 2


async def test_redeem_progress_survives_restart_via_the_store():
    """`redeemed_count_seen` is durable: a new listener (fresh process) does not
    re-fire on_redeem for redeems an earlier run already recorded."""
    add_pending_code(_pending("333333", reusable=True), ACCOUNT)
    payload = {
        "status": "pending",
        "user_id": "@u:hs",
        "redeems": [{"device_id": "D1", "redeemed_at": 1}],
        "redeemed_count": 1,
    }
    rec1 = Recorder()
    await _listener(FakeRegistrar({"333333": [payload]}), rec1)._scan_once()
    assert len(rec1.redeems) == 1

    rec2 = Recorder()  # "restarted" listener — fresh memory, same store
    await _listener(FakeRegistrar({"333333": [payload]}), rec2)._scan_once()
    assert rec2.redeems == []


async def test_expired_code_transitions_and_leaves_the_store():
    add_pending_code(_pending("444444", pair_id="p_abc"), ACCOUNT)
    reg = FakeRegistrar({"444444": [{"status": "expired", "redeems": [], "redeemed_count": 0}]})
    rec = Recorder()
    await _listener(reg, rec)._scan_once()
    assert rec.transitions == [("444444", "expired")]
    assert load_pending_codes(ACCOUNT) == []


async def test_claimed_codes_are_skipped_until_released():
    """An in-process watcher (device.pair_start) claims its code; the listener
    must not double-poll it. A restart clears claims implicitly (memory-only)."""
    add_pending_code(_pending("555555"), ACCOUNT)
    done = {"status": "completed", "user_id": "@u:hs", "redeems": [], "redeemed_count": 0}
    reg = FakeRegistrar({"555555": [done]})
    rec = Recorder()
    listener = _listener(reg, rec)
    listener.claim("555555")
    await listener._scan_once()
    assert reg.calls == []
    listener.release("555555")
    await listener._scan_once()
    assert reg.calls == ["555555"]


async def test_gc_404_drops_the_record():
    """After the registrar's retention GC a code is M_NOT_FOUND — nothing more
    to learn; the listener forgets it rather than polling forever."""
    add_pending_code(_pending("666666"), ACCOUNT)
    reg = FakeRegistrar({"666666": [RegistrarError(404, "M_NOT_FOUND", "gone")]})
    rec = Recorder()
    await _listener(reg, rec)._scan_once()
    assert load_pending_codes(ACCOUNT) == []
    assert rec.transitions == []


async def test_transient_error_keeps_the_record_for_the_next_scan():
    add_pending_code(_pending("777777"), ACCOUNT)
    reg = FakeRegistrar(
        {
            "777777": [
                RegistrarError(429, "M_LIMIT_EXCEEDED", "rate limited"),
                {
                    "status": "completed",
                    "user_id": "@u:hs",
                    "redeems": [{"device_id": "D1", "redeemed_at": 1}],
                    "redeemed_count": 1,
                },
            ]
        }
    )
    rec = Recorder()
    listener = _listener(reg, rec)
    await listener._scan_once()  # transient — retried next scan, record kept
    assert load_pending_codes(ACCOUNT) != []
    await listener._scan_once()
    assert rec.transitions == [("777777", "completed")]


def test_poll_cadence_active_window_then_backoff():
    """C.4 cadence: ~1.5 s while a pairing is actively expected, ≥30 s after."""
    import time

    rec = Recorder()
    listener = _listener(FakeRegistrar({}), rec)
    now_ms = int(time.time() * 1000)
    fresh = _pending("888888", registered_at_ms=now_ms)
    old = _pending("999999", registered_at_ms=now_ms - 3_600_000)
    assert listener._poll_interval(fresh) == 1.5
    assert listener._poll_interval(old) == 30.0
