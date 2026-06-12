"""Transient registrar errors must never kill pairing (live bug, 2026-06-12).

Seen on hermes-test-91: `/pair/status` answered 429 "M_LIMIT_EXCEEDED: status
rate limit exceeded" and `poll_until_complete` died instantly — the pairing
session was lost and the phone hung at "Waiting for your plugin". Transient
errors (429, 502/503/504, network) must retry with exponential backoff inside
the deadline; non-transient errors (other 4xx) must keep failing fast.

Time is faked: `registrar_client.time` is swapped for a fake clock and
`asyncio.sleep` for a clock-advancing no-op, so the backoff schedule is
asserted exactly with zero real waiting.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import chat4000_hermes_plugin.matrix.registrar_client as rc
from chat4000_hermes_plugin.matrix.registrar_client import RegistrarClient, RegistrarError


class _FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    c = _FakeClock()
    # Swap the module's `time` binding (not the global module) for monotonic,
    # and asyncio.sleep (restored by monkeypatch) so backoffs are instant.
    monkeypatch.setattr(rc, "time", SimpleNamespace(monotonic=c.monotonic))
    monkeypatch.setattr(rc.asyncio, "sleep", c.sleep)
    return c


def _err_429() -> RegistrarError:
    return RegistrarError(429, "M_LIMIT_EXCEEDED", "status rate limit exceeded")


def _scripted_client(monkeypatch, outcomes):
    """A RegistrarClient whose `_get` replays `outcomes` (dict → returned,
    exception → raised); the last outcome repeats forever."""
    client = RegistrarClient("https://registrar.example", "svc-token")
    calls = {"n": 0}

    async def fake_get(path, *, auth):
        assert path.startswith("/pair/status?code=")
        assert auth is True
        out = outcomes[min(calls["n"], len(outcomes) - 1)]
        calls["n"] += 1
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(client, "_get", fake_get)
    return client, calls


async def test_poll_survives_429_then_completes(clock, monkeypatch):
    """The live failure: a transient 429 mid-poll must not kill the session —
    the loop backs off, retries, and still reaches `completed`."""
    completed = {"status": "completed", "user_id": "@u:hs", "client_id": "phone-1"}
    client, calls = _scripted_client(
        monkeypatch,
        [_err_429(), {"status": "pending"}, _err_429(), completed],
    )
    result = await client.poll_until_complete("CODE123", deadline_s=300.0)
    assert result == completed
    assert calls["n"] == 4
    # backoff(2 s) → pending → poll interval(1.5 s) → backoff(2 s) → completed
    assert clock.sleeps == [2.0, 1.5, 2.0]


async def test_persistent_429_ends_at_deadline_without_exception(clock, monkeypatch):
    """A registrar that rate-limits forever ends as a timeout (None) at the
    deadline — never an exception, and never a sleep past the deadline budget."""
    client, calls = _scripted_client(monkeypatch, [_err_429()])
    start = clock.now
    result = await client.poll_until_complete("CODE123", deadline_s=120.0)
    assert result is None
    assert calls["n"] > 1  # it kept retrying, not one-shot
    # Exponential schedule: 2, 4, 8, … doubling and capped at 30 s.
    backoffs = [s for s in clock.sleeps if s != 1.5]
    assert backoffs[:4] == [2.0, 4.0, 8.0, 16.0]
    assert max(backoffs) == 30.0
    # The deadline terminated the loop (small overshoot from the final
    # poll-interval sleep is fine; backoff sleeps never overrun the budget).
    assert clock.now - start <= 120.0 + 1.5


async def test_400_fails_fast(clock, monkeypatch):
    """Non-transient 4xx is a protocol error: no retry, no backoff."""
    client, calls = _scripted_client(
        monkeypatch, [RegistrarError(400, "M_INVALID_PARAM", "bad code")]
    )
    with pytest.raises(RegistrarError) as ei:
        await client.poll_until_complete("CODE123", deadline_s=300.0)
    assert ei.value.status == 400
    assert calls["n"] == 1
    assert clock.sleeps == []


async def test_one_shot_status_retries_transient_then_returns(clock, monkeypatch):
    """`status()` itself absorbs a transient burst — so the other poller
    (matrix/commands._poll_pairing, which calls status() directly) also
    survives a 429 without any change on its side."""
    client, calls = _scripted_client(
        monkeypatch,
        [
            _err_429(),
            RegistrarError(502, "M_HOMESERVER_UNAVAILABLE", "registrar down"),
            RegistrarError(0, "M_HOMESERVER_UNAVAILABLE", "could not reach the registrar"),
            {"status": "pending"},
        ],
    )
    result = await client.status("CODE123")
    assert result == {"status": "pending"}
    assert calls["n"] == 4
    assert clock.sleeps == [2.0, 4.0, 8.0]


async def test_one_shot_status_gives_up_after_budget(clock, monkeypatch):
    """A bare status() call is not an infinite loop: after its retry budget
    (~90 s) the transient error surfaces for the caller to judge."""
    client, calls = _scripted_client(monkeypatch, [_err_429()])
    with pytest.raises(RegistrarError) as ei:
        await client.status("CODE123")
    assert ei.value.status == 429
    assert calls["n"] > 1
    assert sum(clock.sleeps) <= 90.0


def test_is_transient_classification():
    for status in (0, 429, 502, 503, 504):
        assert RegistrarError(status, "X", "x").is_transient, status
    for status in (400, 401, 403, 404, 409, 500):
        assert not RegistrarError(status, "X", "x").is_transient, status


async def test_poll_returns_when_a_reusable_code_has_redeems(clock, monkeypatch):
    """C.3: a reusable code never settles to `completed` — a watcher waiting for
    "someone paired" must check `redeems` non-empty, not `status`."""
    paired = {
        "status": "pending",  # stays pending while live (reusable)
        "user_id": "@u:hs",
        "redeems": [{"device_id": "D1", "redeemed_at": 1}],
        "redeemed_count": 1,
        "expires_at": 9999999999999,
    }
    client, calls = _scripted_client(
        monkeypatch, [{"status": "pending", "redeems": [], "redeemed_count": 0}, paired]
    )
    result = await client.poll_until_complete("CODE123", deadline_s=300.0)
    assert result == paired
    assert calls["n"] == 2


async def test_user_ensure_posts_plugin_id_and_parses_result(monkeypatch):
    """C.6.1 — /user/ensure request/response shape."""
    client = RegistrarClient("https://registrar.example", "svc-token")
    seen = {}

    async def fake_post(path, body, *, auth, client_id=None):
        seen.update({"path": path, "body": body, "auth": auth})
        return {"user_id": "@u_one:hs", "created": True}

    monkeypatch.setattr(client, "_post", fake_post)
    result = await client.user_ensure("11111111-2222-3333-4444-555555555555")
    assert seen == {
        "path": "/user/ensure",
        "body": {"plugin_id": "11111111-2222-3333-4444-555555555555"},
        "auth": True,
    }
    assert result.user_id == "@u_one:hs"
    assert result.created is True


async def test_register_sends_reusable_and_ttl_only_when_set(monkeypatch):
    """C.1 wire shape: ttl_seconds/reusable ride only when the caller asked —
    the default register body is unchanged by the redesign."""
    client = RegistrarClient("https://registrar.example", "svc-token")
    bodies = []

    async def fake_post(path, body, *, auth, client_id=None):
        assert path == "/pair/register" and auth is True
        bodies.append(body)
        return {"ok": True, "expires_at": 1}

    monkeypatch.setattr(client, "_post", fake_post)
    await client.register("123456", kind="user", plugin_id="p" * 36)
    await client.register("654321", kind="user", plugin_id="p" * 36, ttl_seconds=600, reusable=True)
    assert bodies[0] == {"code": "123456", "kind": "user", "plugin_id": "p" * 36}
    assert bodies[1] == {
        "code": "654321",
        "kind": "user",
        "plugin_id": "p" * 36,
        "ttl_seconds": 600,
        "reusable": True,
    }
