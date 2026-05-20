"""Pairing-flow logger.

Port of TS src/pairing-logger.ts. Same line shape as runtime_logger, but
writes to a separate file so pairing traces don't get drowned in runtime
traffic. Carries `code` + `room_id` context on every line."""

from __future__ import annotations

import os
import stat
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Optional

from .key_store import resolve_chat4000_plugin_dir
from .log_rotate import rotate_log_if_oversized

PairingLogLevel = Literal["info", "debug"]


def _resolve_pairing_log_path() -> Path:
    return resolve_chat4000_plugin_dir() / "logs" / "pairing.log"


def _now_timestamp() -> str:
    now = datetime.now()
    return now.strftime("%Y-%m-%d %H:%M:%S") + f".{now.microsecond // 1000:03d}"


def _format_value(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, str):
        return f'"{value}"' if " " in value else value
    return str(value)


class PairingLogger:
    _lock = threading.Lock()

    def __init__(self, level: PairingLogLevel, *, room_id: str, code: str):
        self._level = level
        self._room_id = room_id
        self._code = code
        self._log_path = _resolve_pairing_log_path()

    def info(self, event: str, fields: Optional[dict] = None) -> None:
        self._write("INFO", event, fields)

    def debug(self, event: str, fields: Optional[dict] = None) -> None:
        if self._level != "debug":
            return
        self._write("DEBUG", event, fields)

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
        self.info("pair.cancel_remote", {"cancel_origin": "remote",
                                          "reason": (payload or {}).get("reason")})

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

    def _write(self, level: str, event: str, fields: Optional[dict]) -> None:
        merged: dict[str, Any] = {"code": self._code, "room_id": self._room_id}
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
            pass
