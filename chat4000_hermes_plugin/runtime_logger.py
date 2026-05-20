"""Structured runtime logger — thin wrapper over stdlib logging.

Used to write a separate `runtime.log` file. As of 2026-05-20 all
plugin log output funnels into the single rotating file installed by
`logging_setup` (`~/.hermes/plugins/chat4000/logs/chat4000.log`,
10 MB cap). This wrapper preserves the structured key=value shape
callers already use and routes lines through the unified handler.

Never logs plaintext message content — even at debug level.
"""

from __future__ import annotations

import logging
from typing import Literal, Optional

LogLevel = Literal["info", "debug"]

_logger = logging.getLogger("chat4000_hermes_plugin.runtime")


def _format_value(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, str):
        return f'"{value}"' if " " in value else value
    return str(value)


class RuntimeLogger:
    """Per-account logger. One instance per active transport — each line
    carries account_id + group_id context."""

    def __init__(self, level: LogLevel, *, account_id: str, group_id: str):
        self._level = level
        self._account_id = account_id
        self._group_id = group_id

    def info(self, event: str, fields: Optional[dict] = None) -> None:
        self._write(logging.INFO, event, fields)

    def debug(self, event: str, fields: Optional[dict] = None) -> None:
        if self._level != "debug":
            return
        self._write(logging.DEBUG, event, fields)

    def _write(self, level: int, event: str, fields: Optional[dict]) -> None:
        merged: dict = {"account_id": self._account_id, "group_id": self._group_id}
        if fields:
            merged.update(fields)
        details = " ".join(
            f"{k}={_format_value(v)}" for k, v in merged.items() if v not in (None, "")
        )
        line = f"{event}" + (f" {details}" if details else "")
        try:
            _logger.log(level, line)
        except Exception:
            # Logging never breaks runtime behaviour.
            pass
