"""Self-onboard the plugin's Matrix bot identity (mint + persist bot creds).

The bot identity is independent of any user pairing: the plugin registers itself
with the registrar (kind=plugin) and gets a durable Matrix login. This runs at
gateway startup (gateway-first install) so the gateway can connect and bootstrap
its rooms BEFORE anyone pairs — and is reused by `chat4000 pair` / `prepare`.
"""

from __future__ import annotations

import logging

from .matrix.creds_store import BotCreds, load_bot_creds, save_bot_creds

logger = logging.getLogger(__name__)


async def ensure_onboarded(account: str = "default") -> BotCreds | None:
    """Return existing bot creds, or mint + save them via the registrar.

    Returns None if onboarding fails (caller decides whether that's fatal). The
    registrar URL + token are env-aware (stage vs prod) — resolved the same way
    `chat4000 pair` does."""
    creds = load_bot_creds(account)
    if creds is not None:
        return creds

    # Lazy imports: cli pulls in click and is the owner of env/registrar
    # resolution; importing it at module load would be heavy + circular.
    from . import analytics
    from .cli import _gen_code, _registrar, _resolve_env

    reg = _registrar()
    redeemed = await reg.self_onboard(_gen_code(), device_name="hermes-plugin")
    creds = BotCreds(
        user_id=redeemed.user_id,
        device_id=redeemed.device_id,
        access_token=redeemed.access_token,
        gateway_url=redeemed.gateway_url,
        plugin_id=redeemed.plugin_id,
    )
    save_bot_creds(creds, account)
    logger.info("chat4000: self-onboarded bot identity %s", creds.user_id)
    try:
        analytics.track("plugin_onboarded", {"env": _resolve_env(), "via": "boot"})
    except Exception as exc:  # noqa: BLE001
        from .error_log import dump_chat4000_trace

        dump_chat4000_trace("onboarding.analytics", exc)
    return creds
