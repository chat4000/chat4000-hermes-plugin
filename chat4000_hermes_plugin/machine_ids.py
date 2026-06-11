"""Machine identity for analytics (analytics plan v5 — IDN7/IDN8/IDN9).

Two ids, mirroring the phone's two-marker design:

- ``env_id`` (IDN7, the CHURNY one): the existing
  ``~/.config/chat4000/install-id`` owned by ``telemetry.py``. Identifies the
  runtime ENVIRONMENT; dies with a docker rebuild / fresh home. Rides as a
  property on machine events.
- ``agent_install_id`` (IDN8, the STABLE one): THE machine analytics id —
  PostHog ``distinct_id`` and the ``X-Client-Id`` header on registrar calls.
  Lives at ``($HERMES_HOME|~/.hermes)/chat4000-install-id``, next to
  ``state.db``/sessions (the volume-mounted part), so it survives container
  rebuilds AND ``chat4000 uninstall`` (which deletes only
  ``~/.hermes/plugins/chat4000/``). Never deleted by uninstall or
  telemetry-disable — with telemetry off the file is inert and the id never
  rides any wire. The installer mints/reads the SAME file.

IDN9 classifier: at boot, agent_install_id present while the env_id file is
absent (about to be minted fresh) ⇒ the runtime env was rebuilt around the
durable data dir → ``container_rebuilt``. Both fresh ⇒ a genuinely new
machine (no event; the first ``plugin_started`` covers it).
"""

from __future__ import annotations

import contextlib
import os
import uuid
from pathlib import Path

from .key_store import resolve_hermes_home

AGENT_INSTALL_ID_FILENAME = "chat4000-install-id"

_cached_agent_install_id: str | None = None


def agent_install_id_path() -> Path:
    """The durable id file, at the Hermes home ROOT (not the plugin dir)."""
    return resolve_hermes_home() / AGENT_INSTALL_ID_FILENAME


def read_or_mint_agent_install_id() -> str:
    """IDN8: read the durable machine id, minting it on first run (uuid4,
    mode 0600, trailing newline — same format as the env-id file)."""
    global _cached_agent_install_id
    if _cached_agent_install_id:
        return _cached_agent_install_id
    path = agent_install_id_path()
    try:
        if path.exists():
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                _cached_agent_install_id = existing
                return existing
        new_id = str(uuid.uuid4())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_id + "\n", encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
        _cached_agent_install_id = new_id
        return new_id
    except OSError:
        # Read-only / sandboxed fs — fall back to a process-local id (cached
        # so the session's distinct_id at least stays stable).
        _cached_agent_install_id = str(uuid.uuid4())
        return _cached_agent_install_id


def detect_container_rebuilt() -> bool:
    """IDN9: True iff the durable agent_install_id exists but the env-id file
    does not (it is about to be minted fresh) — the docker-rebuild signature.

    MUST be called BEFORE telemetry/analytics initialization, which mints the
    env-id file as a side effect and would erase the freshness signal.
    """
    from .telemetry import INSTALL_ID_PATH

    return _file_has_content(agent_install_id_path()) and not _file_has_content(INSTALL_ID_PATH)


def _file_has_content(path: Path) -> bool:
    try:
        return path.exists() and bool(path.read_text(encoding="utf-8").strip())
    except OSError:
        return False
