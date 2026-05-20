"""Per-account `MessageTransport` registry.

Port of clawconnect-plugin/src/transport/registry.ts. The Hermes plugin
lifecycle has two entry points that need to share one transport:
  - `Chat4000Adapter.connect()` constructs the transport
  - `Chat4000Adapter.send(...)` looks the live transport up by account_id

Multi-account: each account_id gets its own transport. Calling
register_transport with a new instance for an existing account_id
disconnects the stale one (defensive against overlapping start lifecycles
during a Hermes config reload)."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from . import MessageTransport

logger = logging.getLogger(__name__)

_transports: dict[str, MessageTransport] = {}


def register_transport(account_id: str, transport: MessageTransport) -> None:
    existing = _transports.get(account_id)
    if existing is not None and existing is not transport:
        try:
            result = existing.disconnect()
            if asyncio.iscoroutine(result):
                # Best-effort: don't block the register call on the disconnect.
                asyncio.ensure_future(result)
        except Exception:
            logger.exception("stale transport disconnect failed")
    _transports[account_id] = transport


def unregister_transport(account_id: str) -> None:
    _transports.pop(account_id, None)


def get_transport(account_id: str) -> Optional[MessageTransport]:
    return _transports.get(account_id)


def reset_transport_registry_for_tests() -> None:
    _transports.clear()
