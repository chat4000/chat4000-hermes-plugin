"""chat4000 platform plugin for Hermes Agent.

This is the pip-install entry. Hermes' entry-point loader discovers
this package via `hermes_agent.plugins` (see pyproject.toml) and calls
`register(ctx)` on the module returned by `ep.load()`.
"""

from .adapter import register

__all__ = ["register"]
