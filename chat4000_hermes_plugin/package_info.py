"""Resolve the plugin's own version.

When pip-installed, the authoritative source is the installed package metadata
(importlib.metadata) — NOT a pyproject.toml on disk. The old "walk up for any
pyproject.toml" approach picked up the HOST's pyproject (e.g. Hermes' venv) and
reported the wrong version (0.14.0 instead of ours). We try metadata first, then
fall back to OUR pyproject for a source checkout.
"""

from __future__ import annotations

import re
from pathlib import Path

_DIST_NAME = "chat4000-hermes-plugin"
_VERSION_RE = re.compile(r"""^\s*version\s*=\s*["']([^"']+)["']\s*$""", re.MULTILINE)
# Only trust a pyproject that is actually ours.
_NAME_RE = re.compile(r"""^\s*name\s*=\s*["']chat4000-hermes-plugin["']""", re.MULTILINE)


def read_package_version() -> str:
    # 1. Source checkout: walk up for OUR pyproject.toml (name-verified, so we
    #    never grab the host's pyproject — that was the 0.14.0 bug). Fresh, and
    #    absent in a pip install (site-packages has no pyproject) → falls through.
    try:
        here = Path(__file__).resolve()
        for candidate in [here.parent, *here.parents]:
            pyproject = candidate / "pyproject.toml"
            if pyproject.exists():
                text = pyproject.read_text(encoding="utf-8")
                if _NAME_RE.search(text):
                    m = _VERSION_RE.search(text)
                    if m:
                        return m.group(1)
    except Exception:
        pass

    # 2. Installed-package metadata — correct for pip/uv installs.
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version(_DIST_NAME)
        except PackageNotFoundError:
            pass
    except Exception:
        pass
    return "0.0.0"
