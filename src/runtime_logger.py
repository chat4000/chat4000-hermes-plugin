"""Structured runtime logger.

Port of TS src/runtime-logger.ts. Writes structured key=value lines to
`~/.hermes/plugins/chat4000/logs/runtime.log` with 10 MB rotation.

Never logs plaintext message content — even at debug level. The TS impl
goes out of its way to redact bodies and this Python port follows the
same rule. Callers should only log msg_id / seq / inner_t — never body.
"""

from __future__ import annotations

import os
import stat
import threading
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

from .key_store import resolve_chat4000_plugin_dir
from .log_rotate import rotate_log_if_oversized

LogLevel = Literal["info", "debug"]


def _resolve_runtime_log_path() -> Path:
    return resolve_chat4000_plugin_dir() / "logs" / "runtime.log"


def _now_timestamp() -> str:
    now = datetime.now()
    return now.strftime("%Y-%m-%d %H:%M:%S") + f".{now.microsecond // 1000:03d}"


def _format_value(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, str):
        # Quote strings with whitespace so the log line is grep-friendly.
        return f'"{value}"' if " " in value else value
    return str(value)


class RuntimeLogger:
    """Per-account logger. The gateway holds one instance per active
    transport so each log line carries the right account_id + group_id.

    Thread-safe via a process-wide lock — Python's appendFileSync analog
    (os.O_APPEND on POSIX) is atomic up to PIPE_BUF, but log rotation
    needs serialization anyway."""

    _lock = threading.Lock()

    def __init__(self, level: LogLevel, *, account_id: str, group_id: str):
        self._level = level
        self._account_id = account_id
        self._group_id = group_id
        self._log_path = _resolve_runtime_log_path()

    def info(self, event: str, fields: Optional[dict] = None) -> None:
        self._write("INFO", event, fields)

    def debug(self, event: str, fields: Optional[dict] = None) -> None:
        if self._level != "debug":
            return
        self._write("DEBUG", event, fields)

    def _write(self, level: str, event: str, fields: Optional[dict]) -> None:
        merged: dict = {"account_id": self._account_id, "group_id": self._group_id}
        if fields:
            merged.update(fields)
        details = " ".join(
            f"{k}={_format_value(v)}" for k, v in merged.items() if v not in (None, "")
        )
        line = f"{_now_timestamp()} [tid:{threading.get_ident()}] {level} {event}"
        if details:
            line += " " + details
        payload = line + "\n"
        try:
            with self._lock:
                self._log_path.parent.mkdir(parents=True, exist_ok=True)
                rotate_log_if_oversized(self._log_path, len(payload.encode("utf-8")))
                with open(self._log_path, "ab") as f:
                    f.write(payload.encode("utf-8"))
                try:
                    current = stat.S_IMODE(self._log_path.stat().st_mode)
                    if current != 0o600:
                        os.chmod(self._log_path, 0o600)
                except OSError:
                    pass
        except Exception:
            # Logging never breaks runtime behaviour.
            pass
