"""chat4000 v2 platform entry — registers the Matrix-backed adapter with Hermes.

The transport internals live in `matrix/` (gateway, sliding sync, the OlmMachine
binding via crypto_driver, rooms, turns). This file is just the Hermes
registration surface: build the dynamic BasePlatformAdapter subclass, expose
check/validate/env-enablement, and register the platform + CLI.

v1 relay/group-key code is retired — see MIGRATION.md. The real adapter class is
`matrix.hermes_adapter.Chat4000MatrixAdapter`.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from .matrix.creds_store import load_bot_creds
from .matrix.hermes_adapter import Chat4000MatrixAdapter

logger = logging.getLogger(__name__)


def check_requirements() -> bool:
    """Always loadable; the user just won't have a working session until they
    run `chat4000 pair` (which self-onboards the bot identity)."""
    return True


def validate_config(config: object = None) -> bool:
    """Configured enough to connect = we hold a bot login (creds file)."""
    return load_bot_creds("default") is not None


def _env_enablement() -> dict[str, Any] | None:
    """Auto-enable when bot creds exist. The control room is the home channel, but
    we don't know its id until connect/sync — use the bot MXID as a stable id and
    seed CHAT4000_HOME_CHANNEL so Hermes' first-message prompt doesn't fire."""
    creds = load_bot_creds("default")
    if creds is None:
        return None
    if not os.getenv("CHAT4000_HOME_CHANNEL", "").strip():
        os.environ["CHAT4000_HOME_CHANNEL"] = creds.user_id
    return {
        "accountId": "default",
        "groupId": creds.user_id,
        "home_channel": {"chat_id": creds.user_id, "name": "chat4000"},
    }


def _make_adapter_class() -> type:
    """BasePlatformAdapter is only importable inside Hermes — build the real class
    dynamically so this module imports cleanly in tests/CI."""
    from gateway.platforms.base import BasePlatformAdapter

    _SKIP = {"__dict__", "__weakref__", "__module__", "__qualname__"}
    namespace = {k: v for k, v in Chat4000MatrixAdapter.__dict__.items() if k not in _SKIP}
    return type("Chat4000MatrixAdapter", (BasePlatformAdapter,), namespace)


def register(ctx: Any) -> None:  # noqa: ANN401  # Hermes host plugin context (untyped host object)
    """Plugin entry point — Hermes' loader calls this once on discovery."""
    from . import analytics
    from .html_card_tool import register_html_card_tool
    from .plugin_hooks import register_plugin_hooks
    from .telemetry import initialize_chat4000_telemetry

    initialize_chat4000_telemetry()
    analytics.initialize_chat4000_analytics()

    # Hide the transient Telegram "polling conflict" warning a gateway restart
    # triggers (self-heals in seconds) — better UX than making users wait it out.
    from .logging_setup import suppress_telegram_polling_conflict

    suppress_telegram_polling_conflict()

    # Tool bubbles: route Hermes' pre_tool_call to the active adapter's
    # external_tool_start (START-only chat4000.tool events). Self-filters by session.
    register_plugin_hooks(ctx)
    register_html_card_tool(ctx)
    analytics.set_person_properties(
        {
            "plugin_version": analytics.PACKAGE_VERSION,
            "os_platform": __import__("sys").platform,
            "transport": "matrix",
        }
    )

    # chat4000 auth is the Matrix device token + E2EE; no second per-user gate.
    if "CHAT4000_ALLOW_ALL_USERS" not in os.environ:
        os.environ["CHAT4000_ALLOW_ALL_USERS"] = "true"

    AdapterClass = _make_adapter_class()
    ctx.register_platform(
        name="chat4000",
        label="chat4000",
        adapter_factory=lambda cfg: AdapterClass(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        env_enablement_fn=_env_enablement,
        required_env=[],
        install_hint="Run `chat4000 pair` to onboard + pair a device.",
        max_message_length=4096,
        allowed_users_env="CHAT4000_ALLOWED_USERS",
        allow_all_env="CHAT4000_ALLOW_ALL_USERS",
        platform_hint=(
            "You are chatting via chat4000 (encrypted iOS/macOS/CLI client over "
            "Matrix). Markdown is supported; replies stream as message edits and "
            "tool calls render as expandable bubbles. For structured, glanceable, "
            "or delightful final answers use the final_card tool — the native rich "
            "card surface in the chat4000 timeline. Keep tool args readable."
        ),
        emoji="🔐",
    )

    if hasattr(ctx, "register_cli"):
        from .cli import register_chat4000_cli

        register_chat4000_cli(ctx)
