"""Resolve the plugin's own version from pyproject.toml. Lightweight —
no toml parser dependency; we just regex the version line. Falls back
to "0.0.0" if the file is missing (development mode running out of a
checkout that's been moved)."""

from __future__ import annotations

import re
from pathlib import Path

_VERSION_RE = re.compile(r"""^\s*version\s*=\s*["']([^"']+)["']\s*$""", re.MULTILINE)


def read_package_version() -> str:
    try:
        # Walk up from this file until we find pyproject.toml.
        here = Path(__file__).resolve()
        for candidate in [here.parent, *here.parents]:
            pyproject = candidate / "pyproject.toml"
            if pyproject.exists():
                m = _VERSION_RE.search(pyproject.read_text(encoding="utf-8"))
                if m:
                    return m.group(1)
                break
    except Exception:
        pass
    return "0.0.0"
