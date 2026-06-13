"""Bot credentials store — the v2 replacement for the v1 group-key file.

After self-onboarding (registrar `POST /plugins`, C.1) the plugin holds a durable
Matrix bot identity: its MXID, device_id, access_token, and the gateway_url.
These are what `POST /plugins` returns and all the plugin persists — it talks to
the gateway for everything else. There is NO `plugin_id`: the bot MXID is the
plugin identity (protocol B), and the user is DERIVED from it at `PUT /user`.

Stored at ~/.hermes/plugins/chat4000/matrix-creds.json (mode 0600). The Olm/Megolm
key material lives in a SEPARATE SQLite store owned by the pyvodozemac binding —
this file holds only the login, not crypto secrets.

⚠️ Pushback X3: the access_token is treated as durable. If it is ever revoked,
re-onboarding mints a NEW @plugin_ identity (losing the space + rooms). There is
no identity-preserving refresh until the registrar provides one.
"""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from ..key_store import resolve_chat4000_plugin_dir


@dataclass
class BotCreds:
    user_id: str
    device_id: str
    access_token: str
    gateway_url: str

    @property
    def server_name(self) -> str:
        """The homeserver server_name = the part after the colon in the MXID."""
        return self.user_id.split(":", 1)[1] if ":" in self.user_id else ""


def _creds_path(account_id: str = "default") -> Path:
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in (account_id or "default"))
    return resolve_chat4000_plugin_dir() / f"matrix-creds-{safe}.json"


def load_bot_creds(account_id: str = "default") -> BotCreds | None:
    path = _creds_path(account_id)
    if not path.exists():
        return None
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return BotCreds(
            user_id=d["user_id"],
            device_id=d["device_id"],
            access_token=d["access_token"],
            gateway_url=d["gateway_url"],
        )
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        # Missing / unreadable / malformed creds → unconfigured (callers branch).
        return None


def save_bot_creds(creds: BotCreds, account_id: str = "default") -> Path:
    path = _creds_path(account_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(creds), indent=2) + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    return path


def crypto_store_path(account_id: str = "default") -> str:
    """Path the pyvodozemac binding opens its SQLite crypto store at."""
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in (account_id or "default"))
    return str(resolve_chat4000_plugin_dir() / "crypto" / f"{safe}.sqlite")
