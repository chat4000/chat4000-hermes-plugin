"""PostHog product analytics — mirrors `telemetry.py` for crash reports.

Same opt-out semantics as Sentry: `chat4000 telemetry disable`,
`CHAT4000_TELEMETRY_DISABLED=1`, or `--no-telemetry`. One toggle
silences both Sentry and PostHog.

Events fall into three buckets:
  1. Installer (`installer_*`) — fired from scripts/installer.py
  2. Pair lifecycle (`pairing_*`) — fired from pairing.py
  3. Runtime (`gateway_*`, `relay_*`, `message_*`) — adapter.py / transport

distinct_id is the same anonymous UUID Sentry uses
(~/.config/chat4000/install-id), so events + crashes can be correlated
in PostHog's "errors" tab via the Sentry integration.

Event name + property contract is documented (high-level) in the iOS
client repo at docs/analytics-events-proposal.md. Server-side adds:
  - installer_started / installer_package_installed / installer_failed
  - gateway_started / gateway_stopped
  - message_received_text / message_received_image / message_received_audio
  - tool_call_completed
"""

from __future__ import annotations

import logging
import os
import sys
import uuid
from typing import Any

from .package_info import read_package_version

logger = logging.getLogger(__name__)

PACKAGE_VERSION = read_package_version()
_SESSION_ID = str(uuid.uuid4())  # one per Python process
_initialized = False
_disabled_reason: str | None = None
_client: Any = None  # posthog.Posthog instance


def _load_credentials() -> tuple[str, str] | None:
    """Return (api_key, host) from generated module or env, else None."""
    try:
        from . import posthog_dsn_generated  # type: ignore[attr-defined]

        api_key = getattr(posthog_dsn_generated, "POSTHOG_API_KEY", None)
        host = getattr(posthog_dsn_generated, "POSTHOG_HOST", "https://us.i.posthog.com")
        if api_key:
            return (str(api_key).strip(), str(host).strip())
    except ImportError:
        pass
    env_key = os.environ.get("CHAT4000_POSTHOG_API_KEY", "").strip()
    env_host = os.environ.get("CHAT4000_POSTHOG_HOST", "https://us.i.posthog.com").strip()
    return (env_key, env_host) if env_key else None


def initialize_chat4000_analytics() -> None:
    """Initialize the PostHog client. Idempotent. Respects the same
    opt-out paths as Sentry telemetry."""
    global _initialized, _disabled_reason, _client
    if _initialized or _disabled_reason is not None:
        return

    # Reuse the telemetry opt-out check so the two systems share one switch.
    from .telemetry import get_telemetry_status

    status = get_telemetry_status()
    if not status["enabled"]:
        _disabled_reason = status["reason"]
        return

    creds = _load_credentials()
    if creds is None:
        _disabled_reason = "no_credentials"
        return
    api_key, host = creds

    try:
        from posthog import Posthog  # type: ignore[import-not-found]
    except ImportError:
        _disabled_reason = "posthog_sdk_missing"
        return

    try:
        _client = Posthog(
            project_api_key=api_key,
            host=host,
            # Network-level safety: don't let analytics block plugin startup.
            timeout=3,
            disable_geoip=True,
        )
        _initialized = True
        logger.debug(
            "chat4000 analytics initialized (host=%s, session=%s)",
            host,
            _SESSION_ID,
        )
    except Exception as exc:
        _disabled_reason = f"init_failed:{type(exc).__name__}"


def track(event: str, properties: dict[str, Any] | None = None) -> None:
    """Fire a PostHog event. No-op when telemetry is disabled, credentials
    are missing, or the SDK isn't installed."""
    if not _initialized or _client is None:
        # Lazy-init on first track. Cheap if already disabled.
        if _disabled_reason is None:
            initialize_chat4000_analytics()
        if not _initialized or _client is None:
            return

    from .telemetry import _resolve_install_id  # reuse the same install_id

    enriched = _universal_properties()
    if properties:
        enriched.update(properties)
    try:
        _client.capture(
            distinct_id=_resolve_install_id(),
            event=event,
            properties=enriched,
        )
    except Exception as exc:
        # Analytics must never break plugin behavior.
        logger.debug("posthog capture failed: %s", exc)


def flush(timeout: float = 5.0) -> None:
    """Force-flush pending events. Call before process exit / pair completion
    so events from short-lived CLI commands actually land."""
    if not _initialized or _client is None:
        return
    try:
        _client.flush()
    except Exception as exc:
        logger.debug("posthog flush failed: %s", exc)


def shutdown() -> None:
    """Flush + close. Idempotent."""
    global _initialized, _client
    if _client is not None:
        try:
            _client.flush()
            _client.shutdown()
        except Exception:
            pass
    _client = None
    _initialized = False


def set_person_properties(props: dict[str, Any]) -> None:
    """Attach person-level properties to the current install_id. Used for
    cohort-style queries (e.g. plugin version, OS) without spamming every
    event."""
    if not _initialized or _client is None:
        return
    from .telemetry import _resolve_install_id

    try:
        _client.identify(distinct_id=_resolve_install_id(), properties=props)
    except Exception as exc:
        logger.debug("posthog identify failed: %s", exc)


# ─── Internals ────────────────────────────────────────────────────────────


def _universal_properties() -> dict[str, Any]:
    return {
        "source": "hermes-plugin",  # filter events away from iOS client's
        "plugin_version": PACKAGE_VERSION,
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "os_platform": sys.platform,
        "session_id": _SESSION_ID,
        "build_channel": os.environ.get("HERMES_ENV", "production"),
    }
