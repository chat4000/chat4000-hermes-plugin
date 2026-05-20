"""chat4000 platform plugin for Hermes Agent.

This is the pip-install entry. Hermes' entry-point loader discovers
this package via `hermes_agent.plugins` (see pyproject.toml) and calls
`register(ctx)` on the module returned by `ep.load()`.
"""

# Attach the single 10 MB rotating log handler before any submodule
# imports so the first log line lands in the right file.
from .logging_setup import install_plugin_log_handler as _install_log

_install_log()

from .adapter import register  # noqa: E402

__all__ = ["register"]
