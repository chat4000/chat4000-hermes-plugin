"""Single-file log rotation at 10 MB cap.

Port of TS src/log-rotate.ts. Same trim-then-archive policy: when the
next write would push the file past 10 MB, rename it to `.1` (overwriting
any prior `.1`) and start a fresh log. Cheaper than per-line rotation
and good enough for plugin diagnostics."""

from __future__ import annotations

from pathlib import Path

LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB


def rotate_log_if_oversized(
    log_path: Path,
    pending_bytes: int,
    max_bytes: int = LOG_MAX_BYTES,
) -> None:
    try:
        current_size = log_path.stat().st_size
    except OSError:
        return  # log doesn't exist yet — first write
    if current_size + pending_bytes <= max_bytes:
        return
    archive = log_path.with_suffix(log_path.suffix + ".1")
    try:
        if archive.exists():
            archive.unlink()
    except OSError:
        pass
    try:
        log_path.rename(archive)
    except OSError:
        # If rename fails (e.g. cross-device on weird mounts), drop the
        # current file so we don't grow unboundedly.
        try:
            log_path.unlink()
        except OSError:
            pass
