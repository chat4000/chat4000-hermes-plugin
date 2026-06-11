"""Registrar HTTP client (protocol C) — onboarding, pairing, version gate.

Plain HTTPS, no crypto, stdlib only (urllib in a thread → async). This is the
one place the plugin talks to the registrar; everything else goes over the
gateway socket.

Three jobs:
  - self-onboard: register + redeem a `kind=plugin` code → the plugin's own
    `@plugin_…` MXID, durable bot `access_token`, `gateway_url`, `plugin_id`.
  - pair a user: register a `kind=user` code (bound to our `plugin_id`), show it,
    poll `/pair/status` until `completed`, return the user's MXID to invite.
  - version gate: `/version` on boot + before pairing; `force_upgrade` → refuse.

⚠️ Pushback X2: `/pair/register` and `/pair/status` require a shared
`REGISTRAR_SERVICE_TOKEN`. This plugin runs on user machines — a shared secret
there is unprotectable, and `plugin_id` is self-asserted (not validated), so the
per-plugin code limit is fiction. Needs per-plugin tokens. We read the token
from config/env for now and document the risk.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class RedeemResult:
    gateway_url: str
    user_id: str
    device_id: str
    access_token: str
    plugin_id: str | None = None  # only for kind=plugin


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


class RegistrarClient:
    def __init__(
        self, base_url: str, service_token: str | None = None, *, timeout: float = 15.0
    ) -> None:
        self._base = base_url.rstrip("/")
        self._service_token = service_token
        self._timeout = timeout

    # ─── endpoints ────────────────────────────────────────────────────────

    async def register(
        self,
        code: str,
        *,
        kind: str = "user",
        plugin_id: str | None = None,
        user_id: str | None = None,
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        """C.1 — reserve a pairing code. Bearer service token required."""
        body: dict[str, Any] = {"code": code, "kind": kind}
        if plugin_id is not None:
            body["plugin_id"] = plugin_id
        if user_id is not None:
            body["user_id"] = user_id
        if ttl_seconds is not None:
            body["ttl_seconds"] = ttl_seconds
        return await self._post("/pair/register", body, auth=True)

    async def redeem(self, code: str, *, device_name: str | None = None) -> RedeemResult:
        """C.2 — redeem a code (public). Used by the plugin to self-onboard a
        `kind=plugin` code it just registered."""
        body: dict[str, Any] = {"code": code}
        if device_name is not None:
            body["device_name"] = device_name
        r = await self._post("/pair/redeem", body, auth=False)
        return RedeemResult(
            gateway_url=r["gateway_url"],
            user_id=r["user_id"],
            device_id=r["device_id"],
            access_token=r["access_token"],
            plugin_id=r.get("plugin_id"),
        )

    async def status(self, code: str) -> dict[str, Any]:
        """C.3 — poll pairing completion. Bearer service token required."""
        return await self._get(f"/pair/status?code={code}", auth=True)

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
        r = await self._post("/version", body, auth=False, client_id=client_id)
        return VersionVerdict(
            action=r.get("action", "ok"),
            recommended=r.get("recommended"),
            current_terms_version=r.get("current_terms_version"),
            message=r.get("message"),
        )

    async def plugin_version(self, app_id: str, *, client_id: str | None = None) -> PluginVersion:
        """C.5.2 — ask which exact plugin build and install source to run.
        `client_id` as the X-Client-Id header only (PL3); no posthog_id body."""
        r = await self._post("/plugin-version", {"app_id": app_id}, auth=True, client_id=client_id)
        return PluginVersion(current_version=str(r["current_version"]), source=str(r["source"]))

    # ─── high-level flows ─────────────────────────────────────────────────

    async def self_onboard(self, code: str, *, device_name: str = "hermes-plugin") -> RedeemResult:
        """Register + redeem a `kind=plugin` code to mint the plugin's bot
        identity. `plugin_id` is omitted on register (the registrar issues it)."""
        await self.register(code, kind="plugin")
        return await self.redeem(code, device_name=device_name)

    async def poll_until_complete(
        self, code: str, *, interval: float = 1.5, deadline_s: float = 300.0
    ) -> dict[str, Any] | None:
        """Poll `/pair/status` until `completed` (→ returns the full status
        payload: `user_id`, and `client_id` when the redeeming device sent one
        — FLW2), `expired` (→ None), or the deadline. Respects the ≥1 s
        poll-rate rule."""
        waited = 0.0
        while waited < deadline_s:
            s = await self.status(code)
            st = s.get("status")
            if st == "completed":
                return s
            if st == "expired":
                return None
            await asyncio.sleep(interval)
            waited += interval
        return None

    # ─── transport ────────────────────────────────────────────────────────

    async def _post(
        self, path: str, body: dict[str, Any], *, auth: bool, client_id: str | None = None
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self._request, "POST", path, body, auth, client_id)

    async def _get(self, path: str, *, auth: bool) -> dict[str, Any]:
        return await asyncio.to_thread(self._request, "GET", path, None, auth, None)

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None,
        auth: bool,
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
            if not self._service_token:
                raise RegistrarError(401, "M_MISSING_TOKEN", "no service token configured")
            headers["Authorization"] = f"Bearer {self._service_token}"
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
