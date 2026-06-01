"""MatrixSession — the v2 orchestrator.

Owns the live Matrix session for one plugin bot identity: builds the OlmMachine
binding + gateway + crypto driver + room manager, runs the sync loop, decrypts
inbound timeline events, and routes them:

  - `chat4000.command` in the CONTROL room   → on_command  (session.* / plugin.*)
  - `chat4000.command` anywhere else          → IGNORED (the command boundary, E)
  - `m.room.message` in a session room        → on_user_message → Hermes
  - our own events                            → ignored

Outbound replies use `turn_writer(room_id)`. The adapter supplies the two async
callbacks and drives replies; this class knows nothing about Hermes.

Dependencies are built in `start()` but can be injected for tests.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

from .creds_store import BotCreds, crypto_store_path
from .crypto_driver import CryptoDriver, load_olm_machine
from .gateway_client import GatewayClient, GatewayCredentials
from .rooms import RoomManager
from .sliding_sync import build_sync_request
from .turns import TurnWriter

logger = logging.getLogger(__name__)

UserMessageCb = Callable[[str, str, dict], Awaitable[None]]  # (room_id, sender, content)
CommandCb = Callable[[str, str, dict], Awaitable[None]]      # (room_id, command, content)

APP_ID = "@chat4000/hermes-plugin"


async def _noop_msg(room_id: str, sender: str, content: dict) -> None: ...
async def _noop_cmd(room_id: str, command: str, content: dict) -> None: ...


class MatrixSession:
    def __init__(
        self,
        creds: BotCreds,
        *,
        account_id: str = "default",
        plugin_version: str = "0.0.0",
        on_user_message: UserMessageCb = _noop_msg,
        on_command: CommandCb = _noop_cmd,
    ):
        self._creds = creds
        self._account_id = account_id
        self._plugin_version = plugin_version
        self._on_user_message = on_user_message
        self._on_command = on_command

        self.gateway: Optional[GatewayClient] = None
        self.crypto: Optional[CryptoDriver] = None
        self.rooms: Optional[RoomManager] = None
        self._machine = None
        self._members: list[str] = []

    # ─── lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Build the stack and go live. Raises if the pyvodozemac binding isn't
        installed (the wheel must be built — see ../chat4000-pyvodozemac/BUILD.md)."""
        try:
            self._machine = load_olm_machine(
                self._creds.user_id, self._creds.device_id, crypto_store_path(self._account_id)
            )
        except ImportError as exc:
            raise RuntimeError(
                "chat4000_pyvodozemac binding not installed — build the maturin "
                "wheel (see ../chat4000-pyvodozemac/BUILD.md) before connecting"
            ) from exc

        self.gateway = GatewayClient(
            GatewayCredentials(
                gateway_url=self._creds.gateway_url,
                access_token=self._creds.access_token,
                app_id=APP_ID,
                client_version=self._plugin_version,
                platform="linux",
            ),
            on_sync=self._on_sync,
        )
        self.crypto = CryptoDriver(self._machine, self.gateway)
        self.rooms = RoomManager(self.gateway, self._creds.server_name)

        self.gateway.start()
        await self.gateway.wait_authed()
        await self.gateway.start_sync(build_sync_request())

    async def ensure_bootstrap(self) -> None:
        """Create the space + control room if we don't have them yet. Idempotent:
        once the first sync classifies an existing control room, this no-ops."""
        assert self.rooms is not None
        if self.rooms.space_id is None:
            await self.rooms.create_space()
        if self.rooms.control_room_id is None:
            await self.rooms.create_control_room()

    async def stop(self) -> None:
        if self.gateway is not None:
            await self.gateway.close()

    # ─── members / key sharing ────────────────────────────────────────────

    @property
    def members(self) -> list[str]:
        return list(self._members)

    async def invite_user(self, user_id: str) -> None:
        """Invite a paired user into the space + control room (their entry point;
        session rooms are invited as they're created)."""
        if self.rooms is None:
            return
        for r in (self.rooms.space_id, self.rooms.control_room_id):
            if r:
                await self.rooms.invite_user(r, user_id)

    async def set_members(self, user_ids: list[str]) -> None:
        """The user(s) we share room keys with (the paired owner + devices)."""
        self._members = list(user_ids)
        if self.crypto is not None:
            await self.crypto.track_users(self._members)

    def turn_writer(self, room_id: str) -> TurnWriter:
        assert self.crypto is not None and self.gateway is not None
        return TurnWriter(self.crypto, self.gateway, self._members)

    # ─── sync loop + routing ──────────────────────────────────────────────

    async def _on_sync(self, frame: dict) -> None:
        assert self.crypto is not None and self.rooms is not None
        parsed = await self.crypto.process_sync(frame)
        for room_id, r in parsed.rooms.items():
            self.rooms.classify_room(room_id, r.get("required_state", []))
            for ev in r.get("timeline", []):
                if ev.get("type") == "m.room.encrypted":
                    await self._handle_encrypted(room_id, ev)

    async def _handle_encrypted(self, room_id: str, ev: dict) -> None:
        sender = ev.get("sender")
        if self.gateway is not None and sender == self.gateway.user_id:
            return  # ignore our own echoes
        assert self.crypto is not None
        clear = await self.crypto.decrypt(ev, room_id)
        if clear is None:
            return
        content = clear.get("content") or {}
        msgtype = content.get("msgtype")

        # Command boundary (E): only honor chat4000.command in the control room.
        if msgtype == "chat4000.command":
            if self.rooms is not None and room_id == self.rooms.control_room_id:
                await self._on_command(room_id, content.get("command", ""), content)
            else:
                logger.info("ignoring chat4000.command outside control room (%s)", room_id)
            return

        # User message in a session room → up to Hermes.
        if clear.get("type") == "m.room.message" and sender:
            await self._on_user_message(room_id, sender, content)
