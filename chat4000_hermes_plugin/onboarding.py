"""Self-onboard the plugin's Matrix bot identity (mint + persist bot creds).

The bot identity is independent of any user pairing: the plugin births itself
with the registrar (`POST /plugins`, C.1) and gets a durable Matrix login. This
runs at gateway startup (gateway-first install) so the gateway can connect and
bootstrap its rooms BEFORE anyone pairs — and is reused by
`chat4000 pair` / `prepare`. The bot MXID IS the plugin identity (protocol B);
there is no `plugin_id`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .matrix.creds_store import BotCreds, load_bot_creds, save_bot_creds

if TYPE_CHECKING:
    from .matrix.registrar_client import RegistrarClient

logger = logging.getLogger(__name__)


async def ensure_onboarded(
    account: str = "default", registrar: RegistrarClient | None = None
) -> BotCreds | None:
    """Return existing bot creds, or mint + save them via the registrar.

    Returns None if onboarding fails (caller decides whether that's fatal). The
    registrar URL + token are env-aware (stage vs prod) — resolved the same way
    `chat4000 pair` does."""
    creds = load_bot_creds(account)
    if creds is not None:
        # Bind the stored bot token onto the client for the bot-token endpoints
        # (PUT /user, POST /codes, GET /codes — C.4), in case this client built
        # itself without it.
        if registrar is not None:
            registrar.set_bot_token(creds.access_token)
        return creds

    from .registrar_config import build_registrar_client

    reg = registrar or build_registrar_client()
    birth = await reg.self_onboard(device_name="hermes-plugin")
    creds = BotCreds(
        user_id=birth.bot_user_id,
        device_id=birth.device_id,
        access_token=birth.bot_access_token,
        gateway_url=birth.gateway_url,
    )
    save_bot_creds(creds, account)
    logger.info("chat4000: self-onboarded bot identity %s", creds.user_id)
    # DEC3: no plugin_onboarded event — the registrar's plugin_created row
    # (EX-C) is the canonical record of this moment.
    return creds
