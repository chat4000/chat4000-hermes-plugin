"""chat4000 platform plugin for Hermes Agent (directory-install entry).

Hermes' directory-install loader (`hermes plugins install owner/repo`)
clones this entire repo into `~/.hermes/plugins/chat4000/` and loads
this top-level `__init__.py` as `hermes_plugins.chat4000`. It then calls
top-level `register(ctx)`. We re-export from the inner package.

Pip-install path (`pip install chat4000-hermes-plugin`) instead enters
through `chat4000_hermes_plugin/__init__.py` via the
`hermes_agent.plugins` entry-point — see pyproject.toml.
"""

import contextlib

with contextlib.suppress(ImportError):
    # Hermes loads this file via spec_from_file_location with the plugin
    # dir on submodule_search_locations, so the relative subpackage
    # `chat4000_hermes_plugin` resolves cleanly.
    from .chat4000_hermes_plugin.adapter import register  # noqa: F401

__all__ = ["register"] if "register" in dir() else []
