"""Plugin setup (protocol C.6) — one user per plugin.

Setup creates everything the plugin's ONE user will ever need — account, rooms,
invites — BEFORE any device pairs, so pairing afterwards is purely a device
operation. In order (C.6):

  1. birth the bot account (`POST /plugins`, C.1) — the bot MXID is the identity
  2. `PUT /user` (C.2) with the BOT token — create (or return) the plugin's one
     user, whose localpart is DERIVED from the bot MXID; idempotent and
     wipe-proof, never a second account (no `plugin_id`)
  3. open a SHORT-LIVED bot Matrix session and create the workspace space +
     control room (both with `m.room.encryption` at creation; control marked
     `chat4000.room_kind: control`), then invite the user to both
  4. NO key pre-sharing — ever (the single-crypto-owner rule)

The short-lived session here deliberately NEVER loads the OlmMachine: room
creation, room state, and invites are cleartext Matrix *config*; room KEYING is
done exclusively by the live gateway-resident plugin on its next send after a
device joins. We also reuse the bot's one durable device (its stored creds)
rather than minting a second device — same single-crypto-owner outcome, one
fewer identity. The session sends no messages and claims no one-time keys.

Idempotent across re-runs: existing creds/user/rooms/invites are found, not
duplicated (discovery via the homeserver; an invite to an already-joined or
already-invited user is benign).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .matrix.creds_store import BotCreds
from .matrix.users_store import add_known_user
from .onboarding import ensure_onboarded
from .package_info import read_package_version

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager

    from .matrix.registrar_client import RegistrarClient
    from .matrix.rooms import RoomManager

logger = logging.getLogger(__name__)

APP_ID = "@chat4000/hermes-plugin"
SETUP_AUTH_TIMEOUT_S = 30.0

RoomSessionFactory = Callable[[BotCreds], "AbstractAsyncContextManager[RoomManager]"]


@dataclass
class SetupOutcome:
    """Everything `ensure_setup` guarantees exists after it returns."""

    creds: BotCreds
    user_id: str  # the plugin's one user (C.6.1)
    user_created: bool  # False on an idempotent repeat
    space_id: str | None
    control_room_id: str | None


async def ensure_setup(
    account: str = "default",
    *,
    registrar: RegistrarClient | None = None,
    room_session_factory: RoomSessionFactory | None = None,
) -> SetupOutcome | None:
    """Run (or re-run) plugin setup, C.6 steps 1-3. Idempotent. Returns None when
    self-onboarding fails (registrar unreachable — caller decides severity)."""
    from .registrar_config import build_registrar_client

    reg = registrar or build_registrar_client()

    # Step 1 — bot identity (existing creds are reused; never re-onboards).
    # `ensure_onboarded` binds the bot token onto `reg` for the bot-token calls.
    creds = await ensure_onboarded(account, registrar=reg)
    if creds is None:
        return None

    # Step 2 — the plugin's one user (C.2). The user localpart is DERIVED from
    # the bot MXID by the registrar (bot-token auth), so this is idempotent and
    # wipe-proof — no `plugin_id`, no stored binding.
    reg.set_bot_token(creds.access_token)
    ensured = await reg.user_ensure()
    add_known_user(ensured.user_id, account)
    logger.info(
        "chat4000 setup: plugin user %s (%s)",
        ensured.user_id,
        "created" if ensured.created else "already existed",
    )

    # Step 3 — rooms + invites via a short-lived, crypto-free bot session.
    open_session = room_session_factory or _open_gateway_room_session
    async with open_session(creds) as rooms:
        await rooms.discover()
        if rooms.space_id is None:
            await rooms.create_space()
        if rooms.control_room_id is None:
            await rooms.create_control_room()
        for room_id in (rooms.space_id, rooms.control_room_id):
            if room_id:
                await rooms.invite_user(room_id, ensured.user_id)
        space_id, control_room_id = rooms.space_id, rooms.control_room_id

    return SetupOutcome(
        creds=creds,
        user_id=ensured.user_id,
        user_created=ensured.created,
        space_id=space_id,
        control_room_id=control_room_id,
    )


@asynccontextmanager
async def _open_gateway_room_session(creds: BotCreds) -> AsyncIterator[RoomManager]:
    """The C.6 step-3 short-lived bot session: plain C-S calls over the gateway
    socket, no sync, and — critically — no OlmMachine (single-crypto-owner rule)."""
    from .matrix.gateway_client import GatewayClient, GatewayCredentials
    from .matrix.rooms import RoomManager

    async def _ignore_sync(_frame: dict[str, object]) -> None:
        return None

    gateway = GatewayClient(
        GatewayCredentials(
            gateway_url=creds.gateway_url,
            access_token=creds.access_token,
            app_id=APP_ID,
            client_version=read_package_version(),
            platform="linux",
        ),
        on_sync=_ignore_sync,
    )
    gateway.start()
    try:
        await asyncio.wait_for(gateway.wait_authed(), timeout=SETUP_AUTH_TIMEOUT_S)
        yield RoomManager(gateway, creds.server_name)
    finally:
        await gateway.close()
