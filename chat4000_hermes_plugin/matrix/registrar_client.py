"""Registrar HTTP client (protocol C) — onboarding, pairing, version gate.

Plain HTTPS, no crypto, stdlib only (urllib in a thread → async). This is the
one place the plugin talks to the registrar; everything else goes over the
gateway socket.

Two distinct credentials (C.4 "Auth & trust"):
  - the **service token** (`REGISTRAR_SERVICE_TOKEN`, shared) gates ONLY
    `POST /plugins` (C.1, birth a bot) and `POST /plugin-version` (C.5.2);
  - the **bot access token** (per-plugin, from `POST /plugins`) gates
    `PUT /user` (C.2), `POST /codes` (C.3.1), and `GET /codes/{code}` (C.3.3) —
    the registrar whoami-verifies it on every such call.

Jobs:
  - self-onboard: `POST /plugins` (C.1) births the plugin bot → its own
    `@plugin_…` MXID, durable bot `access_token`, `device_id`, `gateway_url`.
    There is NO `plugin_id`: the bot MXID is the identity.
  - create the user: `PUT /user` (C.2), bot-token auth, empty body — the user
    localpart is DERIVED from the bot MXID, so it is idempotent and wipe-proof.
  - pair a device: mint a code `POST /codes` (C.3.1, bot-token), show it, poll
    `GET /codes/{code}` (C.3.3, bot-token) until someone redeems.
  - version gate: `/version` (public) on boot + before pairing; `force_upgrade`
    → refuse.

⚠️ Pushback X2: the shared `REGISTRAR_SERVICE_TOKEN` (now only `POST /plugins`)
ships in a client running on user machines — treat it as public; a leak lets
someone mint inert orphan bots and nothing else (C.1). The privileged
operations (user, codes) are gated by the per-plugin bot token instead.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Transient registrar trouble — worth retrying, never fatal to a poll:
# 429 (rate limit), 502/503/504 (registrar down / dead nginx upstream), and
# 0 (network-level connection errors; see _request's URLError mapping).
_TRANSIENT_HTTP_STATUSES = frozenset({0, 429, 502, 503, 504})

# Exponential backoff for transient errors inside `status()`: start ~2 s,
# double up to 30 s. A bare `status()` call retries for at most
# _STATUS_RETRY_BUDGET_S before the error surfaces; `poll_until_complete`
# overrides the budget with its remaining deadline.
_RETRY_INITIAL_BACKOFF_S = 2.0
_RETRY_MAX_BACKOFF_S = 30.0
_STATUS_RETRY_BUDGET_S = 90.0


@dataclass
class PluginBirth:
    """C.1 `POST /plugins` — a freshly minted plugin bot identity. The bot MXID
    IS the plugin identity; there is no `plugin_id`."""

    bot_user_id: str
    bot_access_token: str
    device_id: str
    gateway_url: str


@dataclass
class UserEnsureResult:
    """C.2 `PUT /user` — the plugin's one DERIVED user (`created` is False on an
    idempotent repeat)."""

    user_id: str
    created: bool


@dataclass
class VersionVerdict:
    action: str  # ok | recommend_upgrade | force_upgrade
    recommended: str | None
    current_terms_version: int | None
    message: str | None


@dataclass
class PluginVersion:
    current_version: str
    source: str


class RegistrarError(RuntimeError):
    def __init__(self, status: int, errcode: str, error: str) -> None:
        super().__init__(f"{status} {errcode}: {error}")
        self.status = status
        self.errcode = errcode

    @property
    def is_transient(self) -> bool:
        """Retryable: rate limit / gateway trouble / network — not a protocol
        error. Other 4xx (bad code, bad token, …) keep failing fast."""
        return self.status in _TRANSIENT_HTTP_STATUSES


def pair_redeem_index(status: dict[str, Any], device_id: object) -> int | None:
    """PL4 `redeem_index` derivation (registry-documented): `GET /codes/{code}`
    (C.3.3) carries no per-entry index, so derive it from the wire fields —
    `redeemed_count − len(redeems) + position(entry) + 1`, with
    `redeemed_count` falling back to `len(redeems)` when absent/0. A `completed`
    shape with no `redeems[]` counts as the single first redeem → 1. None when
    the entry can't be located (never fabricate)."""
    redeems = [e for e in (status.get("redeems") or []) if isinstance(e, dict)]
    if not redeems:
        return 1 if status.get("status") == "completed" else None
    count = int(status.get("redeemed_count") or 0) or len(redeems)
    for pos, entry in enumerate(redeems):
        if entry.get("device_id") == device_id:
            return count - len(redeems) + pos + 1
    return None


class RegistrarClient:
    def __init__(
        self,
        base_url: str,
        service_token: str | None = None,
        *,
        bot_token: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._service_token = service_token
        # The per-plugin bot access token (from POST /plugins). Gates PUT /user,
        # POST /codes, GET /codes/{code} (C.4). Set after self-onboard, or passed
        # in by a caller that already holds the bot creds.
        self._bot_token = bot_token
        self._timeout = timeout

    @property
    def bot_token(self) -> str | None:
        return self._bot_token

    def set_bot_token(self, token: str | None) -> None:
        """Bind the per-plugin bot token used for PUT /user, POST /codes, and
        GET /codes/{code} (C.4). `self_onboard` sets it from the birth response;
        callers that load stored creds set it directly."""
        self._bot_token = token

    # ─── endpoints ────────────────────────────────────────────────────────

    async def create_code(
        self,
        code: str,
        *,
        ttl_seconds: int | None = None,
        reusable: bool = False,
    ) -> dict[str, Any]:
        """C.3.1 `POST /codes` — mint a pairing code. Bearer BOT token (C.4).

        The bound user is the bot's DERIVED user (C.2) — implied by the bot
        token, never named: there is no `kind`, no `plugin_id`, no `user_id`.
        `reusable` codes can be redeemed many times until expiry, each redeem
        adding another device. Response `{ok, expires_at}`."""
        body: dict[str, Any] = {"code": code}
        if ttl_seconds is not None:
            body["ttl_seconds"] = ttl_seconds
        if reusable:
            body["reusable"] = True
        return await self._post("/codes", body, auth=self._bot_auth())

    async def user_ensure(self) -> UserEnsureResult:
        """C.2 `PUT /user` — create (or return) the plugin's one user. Bearer
        BOT token (empty body); the user localpart is DERIVED from the verified
        bot MXID, so this is idempotent and wipe-proof — a repeat returns the
        same `user_id` with `created: false`."""
        r = await self._put("/user", {}, auth=self._bot_auth())
        return UserEnsureResult(user_id=str(r["user_id"]), created=bool(r.get("created", False)))

    async def status(self, code: str) -> dict[str, Any]:
        """C.3.3 `GET /codes/{code}` — poll pairing completion. Bearer BOT token.

        Transient registrar trouble (429 rate limit, 502/503/504, network
        errors) is retried in place with exponential backoff (~2 s doubling
        to 30 s, ≤ _STATUS_RETRY_BUDGET_S per call) — seen live: a single
        status 429 (M_LIMIT_EXCEEDED) used to kill the pairing watcher and
        lose the session. Non-transient errors still raise immediately."""
        return await self._status_with_retry(code, budget_s=_STATUS_RETRY_BUDGET_S)

    async def _status_with_retry(self, code: str, *, budget_s: float) -> dict[str, Any]:
        """One `GET /codes/{code}` call that absorbs transient errors for up to
        `budget_s` seconds (backoff sleeps never overrun the budget; the
        first attempt is always made). On a still-failing registrar the last
        transient error is raised for the caller's deadline to judge."""
        give_up_at = time.monotonic() + budget_s
        backoff = _RETRY_INITIAL_BACKOFF_S
        auth = self._bot_auth()
        while True:
            try:
                return await self._get(f"/codes/{code}", auth=auth)
            except RegistrarError as e:
                if not e.is_transient or time.monotonic() + backoff > give_up_at:
                    raise
                logger.warning(
                    "transient registrar error on GET /codes (%s) — retrying in %.0f s",
                    e,
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, _RETRY_MAX_BACKOFF_S)

    async def version(
        self,
        app_id: str,
        client_version: str,
        release_channel: str,
        platform: str = "linux",
        client_id: str | None = None,
    ) -> VersionVerdict:
        """C.5.1 — version policy check. Public. `client_id` (the machine's
        agent_install_id, PL3) rides ONLY as the X-Client-Id header — the old
        posthog_id body field is gone (never send both)."""
        body = {
            "app_id": app_id,
            "client_version": client_version,
            "release_channel": release_channel,
            "platform": platform,
        }
        r = await self._post("/version", body, auth=None, client_id=client_id)
        return VersionVerdict(
            action=r.get("action", "ok"),
            recommended=r.get("recommended"),
            current_terms_version=r.get("current_terms_version"),
            message=r.get("message"),
        )

    async def plugin_version(self, app_id: str, *, client_id: str | None = None) -> PluginVersion:
        """C.5.2 — ask which exact plugin build and install source to run. Bearer
        SERVICE token. `client_id` as the X-Client-Id header only (PL3); no
        posthog_id body."""
        r = await self._post(
            "/plugin-version", {"app_id": app_id}, auth=self._service_auth(), client_id=client_id
        )
        return PluginVersion(current_version=str(r["current_version"]), source=str(r["source"]))

    # ─── high-level flows ─────────────────────────────────────────────────

    async def self_onboard(self, *, device_name: str = "hermes-plugin") -> PluginBirth:  # noqa: ARG002
        """C.1 `POST /plugins` — birth the plugin bot. Bearer SERVICE token,
        empty body. NOT idempotent: every call mints a fresh bot, so call it
        exactly once at first self-onboard and persist what it returns. The
        returned bot token is bound onto this client for the subsequent
        bot-token calls (PUT /user, POST /codes). `device_name` is accepted for
        call-site compatibility but unused — C.1 takes no body."""
        r = await self._post("/plugins", {}, auth=self._service_auth())
        birth = PluginBirth(
            bot_user_id=str(r["bot_user_id"]),
            bot_access_token=str(r["bot_access_token"]),
            device_id=str(r["device_id"]),
            gateway_url=str(r["gateway_url"]),
        )
        self.set_bot_token(birth.bot_access_token)
        return birth

    async def poll_until_complete(
        self, code: str, *, interval: float = 1.5, deadline_s: float = 300.0
    ) -> dict[str, Any] | None:
        """Poll `GET /codes/{code}` until someone paired, the code expired
        (→ None), or the deadline (→ None).

        "Someone paired" (C.3) is `status == completed` OR `redeems` non-empty —
        a REUSABLE code never settles to `completed` however many redeems it
        has, so a watcher must check `redeems`, not `status`. On success the
        full status payload is returned (`user_id`, `redeems`/`redeemed_count`,
        and `client_id` when the redeeming device sent one — FLW2). Respects
        the ≥1 s poll-rate rule.

        Transient registrar errors (429/502/503/504/network) never abort the
        loop — the pairing session must survive a momentary rate limit. They
        are backed off and retried inside the remaining deadline budget; a
        registrar still broken at the deadline ends as a timeout (None),
        exactly like a code nobody redeemed. Non-transient errors raise."""
        deadline = time.monotonic() + deadline_s
        while time.monotonic() < deadline:
            try:
                s = await self._status_with_retry(
                    code, budget_s=max(0.0, deadline - time.monotonic())
                )
            except RegistrarError as e:
                if not e.is_transient:
                    raise
                # Expected + handled: keep polling until the deadline says stop.
                logger.warning("registrar still unavailable while pairing (%s) — polling on", e)
                await asyncio.sleep(interval)
                continue
            st = s.get("status")
            if st == "completed" or s.get("redeems"):
                return s
            if st == "expired":
                return None
            await asyncio.sleep(interval)
        return None

    # ─── auth selectors (C.4 "Auth & trust — two distinct credentials") ────

    def _service_auth(self) -> str:
        """The shared service token — gates POST /plugins and POST /plugin-version."""
        if not self._service_token:
            raise RegistrarError(401, "M_MISSING_TOKEN", "no service token configured")
        return self._service_token

    def _bot_auth(self) -> str:
        """The per-plugin bot token — gates PUT /user, POST /codes, GET /codes."""
        if not self._bot_token:
            raise RegistrarError(
                401,
                "M_MISSING_TOKEN",
                "no bot access token configured (self-onboard first, C.1)",
            )
        return self._bot_token

    # ─── transport ────────────────────────────────────────────────────────

    async def _post(
        self, path: str, body: dict[str, Any], *, auth: str | None, client_id: str | None = None
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self._request, "POST", path, body, auth, client_id)

    async def _put(
        self, path: str, body: dict[str, Any], *, auth: str | None, client_id: str | None = None
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self._request, "PUT", path, body, auth, client_id)

    async def _get(self, path: str, *, auth: str | None) -> dict[str, Any]:
        return await asyncio.to_thread(self._request, "GET", path, None, auth, None)

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None,
        auth: str | None,
        client_id: str | None = None,
    ) -> dict[str, Any]:
        url = self._base + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"}
        if client_id:
            # PL3: the machine analytics id (agent_install_id). Callers pass
            # None when telemetry is disabled — the id then never rides.
            headers["X-Client-Id"] = client_id[:64]
        if auth:
            headers["Authorization"] = f"Bearer {auth}"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)  # noqa: S310  # our own registrar endpoint (default https; override is operator-controlled)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # noqa: S310  # our own registrar endpoint (default https; override is operator-controlled)
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                payload = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                payload = {}
            # Don't surface a raw HTML body (e.g. an nginx 502 page) as the error
            # message — give a short, classified message instead.
            errcode = payload.get("errcode") or (
                "M_HOMESERVER_UNAVAILABLE" if e.code in (502, 503, 504) else "M_UNKNOWN"
            )
            error = payload.get("error") or (
                f"registrar returned HTTP {e.code} with no JSON body "
                "(the registrar service may be down or unreachable behind nginx)"
            )
            raise RegistrarError(e.code, errcode, error) from e
        except urllib.error.URLError as e:
            raise RegistrarError(
                0,
                "M_HOMESERVER_UNAVAILABLE",
                f"could not reach the registrar at {self._base}: {e.reason}",
            ) from e
