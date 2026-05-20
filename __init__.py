"""chat4000 platform plugin for Hermes Agent.

Hermes loads this file as `hermes_plugins.chat4000` and looks for a
top-level `register(ctx)` callable. We re-export from `src.adapter`
where the actual implementation lives."""

from .src.adapter import register

__all__ = ["register"]
