"""Error / trace dump utility.

Port of TS src/error-log.ts. Writes the exception type, message, stack,
and a small context dict to `~/.hermes/plugins/chat4000/logs/errors.log`.
Also forwards to Sentry via `telemetry.capture_chat4000_exception` so
crashes/handled-errors show up in production telemetry."""

from __future__ import annotations

import json
import os
import stat
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from .key_store import resolve_chat4000_plugin_dir
from .log_rotate import rotate_log_if_oversized
from .telemetry import capture_chat4000_exception


def resolve_chat4000_error_log_path() -> Path:
    return resolve_chat4000_plugin_dir() / "logs" / "errors.log"


_lock = threading.Lock()


def dump_chat4000_trace(
    scope: str,
    error: BaseException,
    context: dict[str, Any] | None = None,
) -> Path:
    capture_chat4000_exception(error, scope=scope)

    log_path = resolve_chat4000_error_log_path()
    lines = [
        f"=== {datetime.utcnow().isoformat()}Z [{scope}] ===",
        f"message: {error}",
    ]
    if context:
        try:
            lines.append("context: " + json.dumps(context, default=str))
        except Exception:
            lines.append(f"context: {context}")

    try:
        tb = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        lines.append(tb.rstrip())
    except Exception:
        pass

    lines.append("")  # blank separator
    payload = ("\n".join(lines) + "\n").encode("utf-8")

    try:
        with _lock:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            rotate_log_if_oversized(log_path, len(payload))
            with open(log_path, "ab") as f:
                f.write(payload)
            try:
                current = stat.S_IMODE(log_path.stat().st_mode)
                if current != 0o600:
                    os.chmod(log_path, 0o600)
            except OSError:
                pass
    except Exception:
        pass
    return log_path
