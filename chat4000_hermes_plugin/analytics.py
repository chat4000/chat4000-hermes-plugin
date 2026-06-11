"""PostHog product analytics — mirrors `telemetry.py` for crash reports.

Same opt-out semantics as Sentry: `chat4000 telemetry disable`,
`CHAT4000_TELEMETRY_DISABLED=1`, or `--no-telemetry`. One toggle
silences both Sentry and PostHog.

Events fall into three buckets:
  1. Installer (`installer_*`) — fired from scripts/installer.py
  2. Pair lifecycle (`pairing_*`) — fired from pairing.py
  3. Runtime (`gateway_*`, `relay_*`, `message_*`) — adapter.py / transport

distinct_id is the STABLE machine id `agent_install_id` (plan v5 IDN8,
minted by machine_ids.py at the Hermes home root so it survives docker
rebuilds and plugin uninstalls). The churny env id
(~/.config/chat4000/install-id, IDN7) rides as the `env_id` property on
every event; `paired_client_id` (FLW4, latest pairing wins) rides as an
emulated super property the same way. Events go to the self-hosted
PostHog instance only (INF5).

Event name + property contract is documented (high-level) in the iOS
client repo at docs/analytics-events-proposal.md. Server-side adds:
  - installer_started / installer_package_installed / installer_failed
  - gateway_started / gateway_stopped
  - message_received_text / message_received_image / message_received_audio
  - tool_call_completed
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from .package_info import read_package_version

logger = logging.getLogger(__name__)

PACKAGE_VERSION = read_package_version()
_SESSION_ID = str(uuid.uuid4())  # one per Python process
_initialized = False
_disabled_reason: str | None = None
_client: Any = None  # posthog.Posthog instance


# Self-hosted PostHog only (plan v5 INF5) — the US-cloud default is gone.
_SELF_HOSTED_POSTHOG = "https://posthog.chat4000.com"


def _load_credentials() -> tuple[str, str] | None:
    """Return (api_key, host) from generated module or env, else None."""
    try:
        from . import posthog_dsn_generated

        api_key = getattr(posthog_dsn_generated, "POSTHOG_API_KEY", None)
        host = getattr(posthog_dsn_generated, "POSTHOG_HOST", _SELF_HOSTED_POSTHOG)
        if api_key:
            return (str(api_key).strip(), str(host).strip())
    except ImportError:
        pass
    env_key = os.environ.get("CHAT4000_POSTHOG_API_KEY", "").strip()
    env_host = os.environ.get("CHAT4000_POSTHOG_HOST", _SELF_HOSTED_POSTHOG).strip()
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
        from posthog import Posthog
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
    except Exception as exc:  # noqa: BLE001
        # Analytics init must never break startup; record why it's off and
        # report the unexpected failure once to the sink.
        _disabled_reason = f"init_failed:{type(exc).__name__}"
        from .error_log import dump_chat4000_trace

        dump_chat4000_trace("analytics_init", exc)


def track(event: str, properties: dict[str, Any] | None = None) -> None:
    """Fire a PostHog event. No-op when telemetry is disabled, credentials
    are missing, or the SDK isn't installed."""
    if not _initialized or _client is None:
        # Lazy-init on first track. Cheap if already disabled.
        if _disabled_reason is None:
            initialize_chat4000_analytics()
        if not _initialized or _client is None:
            return

    from .machine_ids import read_or_mint_agent_install_id

    enriched = _universal_properties()
    if properties:
        enriched.update(properties)
    try:
        _client.capture(
            distinct_id=read_or_mint_agent_install_id(),  # IDN8 — the stable machine id
            event=event,
            properties=enriched,
        )
    except Exception as exc:  # noqa: BLE001
        # Analytics must never break plugin behavior — report once to the sink.
        from .error_log import dump_chat4000_trace

        dump_chat4000_trace("analytics_capture", exc)


def flush(timeout: float = 5.0) -> None:
    """Force-flush pending events. Call before process exit / pair completion
    so events from short-lived CLI commands actually land."""
    if not _initialized or _client is None:
        return
    try:
        _client.flush()
    except Exception as exc:  # noqa: BLE001
        from .error_log import dump_chat4000_trace

        dump_chat4000_trace("analytics_flush", exc)


def shutdown() -> None:
    """Flush + close. Idempotent."""
    global _initialized, _client
    if _client is not None:
        try:
            _client.flush()
            _client.shutdown()
        except Exception as exc:  # noqa: BLE001
            from .error_log import dump_chat4000_trace

            dump_chat4000_trace("analytics_shutdown", exc)
    _client = None
    _initialized = False


def set_person_properties(props: dict[str, Any]) -> None:
    """Attach person-level properties to the current install_id. Used for
    cohort-style queries (e.g. plugin version, OS) without spamming every
    event."""
    if not _initialized or _client is None:
        return
    from .machine_ids import read_or_mint_agent_install_id

    try:
        # posthog >=7 dropped the client's `identify`; person properties are set
        # via `set(distinct_id, properties)` (present since 3.x).
        _client.set(distinct_id=read_or_mint_agent_install_id(), properties=props)
    except Exception as exc:  # noqa: BLE001
        from .error_log import dump_chat4000_trace

        dump_chat4000_trace("analytics_set_person", exc)


# ─── Identity helpers (plan v5: IDN7/IDN8, FLW4, PL3) ──────────────────────


def machine_client_id() -> str | None:
    """The agent_install_id for `X-Client-Id` registrar headers (PL3) — or
    None when telemetry is off, so the id never rides any wire then."""
    from .telemetry import get_telemetry_status

    if not get_telemetry_status()["enabled"]:
        return None
    from .machine_ids import read_or_mint_agent_install_id

    return read_or_mint_agent_install_id()


def _paired_client_id_path() -> Path:
    from .key_store import resolve_chat4000_plugin_dir

    return resolve_chat4000_plugin_dir() / "paired-client-id"


def register_paired_client_id(client_id: str) -> None:
    """FLW4: persist the paired phone's client_id as an emulated super
    property — latest pairing wins; every subsequent plugin event carries it
    via `_universal_properties`. Best-effort: an unwritable store only costs
    the join property, never the event."""
    value = str(client_id).strip()[:64]
    if not value:
        return
    try:
        path = _paired_client_id_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value + "\n", encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
    except OSError as exc:
        from .error_log import dump_chat4000_trace

        dump_chat4000_trace("analytics_paired_client_id", exc)


def _load_paired_client_id() -> str | None:
    try:
        value = _paired_client_id_path().read_text(encoding="utf-8").strip()
        return value or None
    except OSError:
        return None


# ─── Boot events (plan v5: PL1 + PL5) ───────────────────────────────────────


def emit_plugin_boot_analytics(*, container_rebuilt: bool) -> None:
    """PL1 `plugin_started` once per boot (+ PL5 `container_rebuilt` first
    when the IDN9 classifier fired). plugin_version / env_id /
    paired_client_id ride via the universal properties."""
    if container_rebuilt:
        track("container_rebuilt", {})
    track(
        "plugin_started",
        {"agent_kind": "hermes", "agent_version": _host_agent_version()},
    )


def _host_agent_version() -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("hermes-agent")
        except PackageNotFoundError:
            return "unknown"
    except ImportError:
        return "unknown"


# ─── Internals ────────────────────────────────────────────────────────────


def _universal_properties() -> dict[str, Any]:
    from .telemetry import _resolve_install_id

    props = {
        "source": "hermes-plugin",  # filter events away from iOS client's
        "plugin_version": PACKAGE_VERSION,
        "python_version": (
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        ),
        "os_platform": sys.platform,
        "session_id": _SESSION_ID,
        "build_channel": os.environ.get("HERMES_ENV", "production"),
        # IDN7: the churny environment id rides as a property on every event.
        "env_id": _resolve_install_id(),
    }
    paired = _load_paired_client_id()
    if paired:
        props["paired_client_id"] = paired  # FLW4 emulated super property
    return props
