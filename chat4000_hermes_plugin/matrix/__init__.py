"""Matrix v2 transport layer for the chat4000 Hermes plugin.

Owns the gateway WebSocket, sliding sync, the crypto driver (which drives the
`chat4000_pyvodozemac` OlmMachine binding), and the registrar HTTP client.

This replaces the v1 relay stack (`transport/relay.py`, `crypto.py`,
`pairing.py`, `ack_store.py`, `recv_ack_batcher.py`) — see MIGRATION.md.
"""

from __future__ import annotations

from .commands import CommandHandler
from .creds_store import BotCreds, crypto_store_path, load_bot_creds, save_bot_creds
from .crypto_driver import CryptoDriver, load_olm_machine
from .gateway_client import AuthError, GatewayClient, GatewayCredentials
from .registrar_client import (
    RedeemResult,
    RegistrarClient,
    RegistrarError,
    VersionVerdict,
)
from .rooms import RoomManager
from .session import MatrixSession
from .sliding_sync import build_sync_request, parse_sync_frame
from .turns import TurnWriter

__all__ = [
    # transport
    "GatewayClient",
    "GatewayCredentials",
    "AuthError",
    # registrar
    "RegistrarClient",
    "RedeemResult",
    "RegistrarError",
    "VersionVerdict",
    # crypto + sync
    "CryptoDriver",
    "load_olm_machine",
    "build_sync_request",
    "parse_sync_frame",
    # rooms / turns / commands
    "RoomManager",
    "TurnWriter",
    "CommandHandler",
    # session + creds
    "MatrixSession",
    "BotCreds",
    "load_bot_creds",
    "save_bot_creds",
    "crypto_store_path",
]
