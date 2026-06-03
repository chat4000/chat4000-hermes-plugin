"""Single-file rotating log for the entire plugin.

One file, hard 10 MB cap, rolls on itself when exceeded.

Path:     ~/.hermes/plugins/chat4000/logs/chat4000.log
Rotation: `RotatingFileHandler(maxBytes=10*1024*1024, backupCount=0)`.
          When the file hits 10 MB, the handler truncates and restarts.
          backupCount=0 means there is NO `.1` archive — strict 10 MB
          total disk footprint regardless of how long the plugin runs.

The handler is attached to the `chat4000_hermes_plugin` namespace root,
so every sub-module's `logger = logging.getLogger(__name__)` writes
here automatically. `propagate=True` is preserved so the same lines
still surface in Hermes' `agent.log` for cross-cutting visibility.

Idempotent: safe to call multiple times across plugin re-imports
(Hermes' discovery + entry-point loader can both touch the package).
"""

from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

from .key_store import resolve_chat4000_plugin_dir

_LOG_BYTES_CAP = 10 * 1024 * 1024  # 10 MB strict ceiling
_HANDLER_ATTR = "_chat4000_plugin_handler_installed"


def _resolve_level() -> int:
    """Log level from CHAT4000_LOG_LEVEL (default INFO). Set it to DEBUG on a test
    box to surface the command-path / session-creation / status-cadence diagnostics
    (which log at DEBUG so production stays quiet)."""
    name = os.environ.get("CHAT4000_LOG_LEVEL", "INFO").strip().upper()
    level = logging.getLevelName(name)
    return level if isinstance(level, int) else logging.INFO


def install_plugin_log_handler() -> Path:
    """Attach the rotating handler to the plugin namespace root.

    Returns the resolved log path. No-op on subsequent calls."""
    pkg_logger = logging.getLogger("chat4000_hermes_plugin")
    if getattr(pkg_logger, _HANDLER_ATTR, False):
        return resolve_log_path()

    log_path = resolve_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=_LOG_BYTES_CAP,
        backupCount=0,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    level = _resolve_level()
    handler.setLevel(level)

    pkg_logger.addHandler(handler)
    if pkg_logger.level == logging.NOTSET or pkg_logger.level > level:
        pkg_logger.setLevel(level)
    setattr(pkg_logger, _HANDLER_ATTR, True)
    return log_path


def resolve_log_path() -> Path:
    return resolve_chat4000_plugin_dir() / "logs" / "chat4000.log"


def suppress_telegram_polling_conflict() -> None:
    """Drop ONLY the transient Telegram 'polling conflict (terminated by other
    getUpdates request)' warning from Hermes' telegram platform logger.

    It appears for a few seconds after a gateway restart (the old getUpdates
    poll hasn't been released yet) and then self-heals — pure noise that hurts
    UX. We attach a filter that drops just that message; every other Telegram
    log line is kept. Opt out with CHAT4000_SUPPRESS_TELEGRAM_CONFLICT=0."""
    import os

    if os.environ.get("CHAT4000_SUPPRESS_TELEGRAM_CONFLICT", "1").strip().lower() in (
        "0",
        "false",
        "no",
    ):
        return

    class _ConflictFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
            try:
                msg = record.getMessage()
            except (TypeError, ValueError):
                # Bad %-format args in the record — let it through unfiltered.
                return True
            return not ("polling conflict" in msg or "terminated by other getUpdates" in msg)

    logging.getLogger("gateway.platforms.telegram").addFilter(_ConflictFilter())
