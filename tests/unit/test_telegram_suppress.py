"""The Telegram polling-conflict log suppression filter."""

from __future__ import annotations

import logging

from chat4000_hermes_plugin.logging_setup import suppress_telegram_polling_conflict

LOGGER = "gateway.platforms.telegram"


def _record(msg: str) -> logging.LogRecord:
    return logging.LogRecord(LOGGER, logging.WARNING, __file__, 1, msg, None, None)


def test_drops_only_the_conflict_message(monkeypatch):
    monkeypatch.delenv("CHAT4000_SUPPRESS_TELEGRAM_CONFLICT", raising=False)
    logging.getLogger(LOGGER).filters.clear()
    suppress_telegram_polling_conflict()
    f = logging.getLogger(LOGGER).filters[-1]
    conflict = _record(
        "[Telegram] Telegram polling conflict (1/3), will retry in 10s. "
        "Error: Conflict: terminated by other getUpdates request"
    )
    other = _record("[Telegram] some unrelated warning")
    assert f.filter(conflict) is False  # dropped
    assert f.filter(other) is True  # kept
    logging.getLogger(LOGGER).filters.clear()


def test_opt_out_adds_no_filter(monkeypatch):
    monkeypatch.setenv("CHAT4000_SUPPRESS_TELEGRAM_CONFLICT", "0")
    logging.getLogger(LOGGER).filters.clear()
    suppress_telegram_polling_conflict()
    assert logging.getLogger(LOGGER).filters == []
