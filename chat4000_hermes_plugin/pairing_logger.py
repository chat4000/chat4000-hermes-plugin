"""Pairing-flow logger — thin wrapper over stdlib logging.

Previously wrote to a separate `pairing.log` file. As of 2026-05-20 all
plugin log output funnels into the single rotating file installed by
`logging_setup` (`~/.hermes/plugins/chat4000/logs/chat4000.log`,
10 MB cap). This wrapper preserves the structured key=value shape
callers use and routes through the unified handler.

Carries `code` + `room_id` context on every line.
"""

from __future__ import annotations

import logging
from typing import Any, Literal, Optional

PairingLogLevel = Literal["info", "debug"]

_logger = logging.getLogger("chat4000_hermes_plugin.pairing")


def _format_value(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, str):
        return f'"{value}"' if " " in value else value
    return str(value)


class PairingLogger:
    def __init__(self, level: PairingLogLevel, *, room_id: str, code: str):
        self._level = level
        self._room_id = room_id
        self._code = code

    def info(self, event: str, fields: Optional[dict] = None) -> None:
        self._write(logging.INFO, event, fields)

    def debug(self, event: str, fields: Optional[dict] = None) -> None:
        if self._level != "debug":
            return
        self._write(logging.DEBUG, event, fields)

    def log_send(self, envelope: dict, fields: Optional[dict] = None) -> None:
        payload_t = self._extract_payload_t(envelope)
        merged = {"direction": "send", "type": envelope.get("type"), "payload_t": payload_t}
        if fields:
            merged.update(fields)
        self.info("pair.send", merged)

    def log_recv(self, envelope: dict, fields: Optional[dict] = None) -> None:
        payload_t = self._extract_payload_t(envelope)
        merged = {"direction": "recv", "type": envelope.get("type"), "payload_t": payload_t}
        if fields:
            merged.update(fields)
        self.info("pair.recv", merged)

    def log_cancel_remote(self, payload: Optional[dict]) -> None:
        self.info(
            "pair.cancel_remote",
            {"cancel_origin": "remote", "reason": (payload or {}).get("reason")},
        )

    def log_cancel_local(self, reason: str) -> None:
        self.info("pair.cancel_local", {"cancel_origin": "local", "reason": reason})

    def log_ws_close(self, code: int, reason: str) -> None:
        self.info("pair.ws_close", {"close_code": code, "close_reason": reason})

    def log_ws_error(self, error: BaseException) -> None:
        self.info("pair.ws_error", {"error": str(error)})

    def log_finish(self, outcome: str, reason: str) -> None:
        self.info("pair.finish", {"outcome": outcome, "reason": reason})

    @staticmethod
    def _extract_payload_t(envelope: dict) -> Optional[str]:
        if envelope.get("type") != "pair_data":
            return None
        return (envelope.get("payload") or {}).get("t")

    def _write(self, level: int, event: str, fields: Optional[dict]) -> None:
        merged: dict[str, Any] = {"code": self._code, "room_id": self._room_id}
        if fields:
            merged.update(fields)
        details = " ".join(
            f"{k}={_format_value(v)}" for k, v in merged.items() if v not in (None, "")
        )
        line = f"{event}" + (f" {details}" if details else "")
        try:
            _logger.log(level, line)
        except Exception:
            pass
