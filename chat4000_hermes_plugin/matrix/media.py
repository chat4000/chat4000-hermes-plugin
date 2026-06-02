"""Encrypted media attachments (protocol D.3).

Media never crosses the WS — it rides Matrix's media repo over HTTP, reverse-
proxied on the gateway host. Blobs are end-to-end encrypted by the SENDER with
AES-256-CTR; only the ciphertext is uploaded (the homeserver sees an opaque
blob). The per-file AES key + IV + hash travel inside the room event's `file`
object, which itself sits inside `m.room.encrypted` — so the key is E2EE too.

This module is the AES-CTR encrypt/decrypt core (pure, testable) plus a thin
HTTP client for upload/download against the gateway-host media paths, using the
bot's own access token. The Olm/Megolm layer (the binding) is NOT involved —
once the room event is decrypted, the `file` object is cleartext and we work
from it directly.

Flow:
  outbound: encrypt_attachment(bytes) → upload ciphertext → mxc:// → build the
            `file` object → put it in an m.image/m.audio event (crypto driver).
  inbound:  read `file` from the decrypted event → download ciphertext →
            verify sha256 → decrypt_attachment → bytes (cache → vision/STT).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _b64url_nopad(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64url_unpad_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ─── AES-256-CTR core (pure, no I/O) ───────────────────────────────────────


def encrypt_attachment(data: bytes) -> tuple[bytes, dict[str, Any]]:
    """Encrypt `data` → (ciphertext, file_meta). `file_meta` is the EncryptedFile
    object minus `url` (the caller fills `url` after upload)."""
    key = secrets.token_bytes(32)
    # IV: 16 bytes; high 8 random, low 8 (the CTR counter) start at 0 (Matrix spec).
    iv = secrets.token_bytes(8) + b"\x00" * 8
    enc = Cipher(algorithms.AES(key), modes.CTR(iv)).encryptor()
    ciphertext = enc.update(data) + enc.finalize()
    file_meta = {
        "v": "v2",
        "key": {
            "kty": "oct",
            "alg": "A256CTR",
            "key_ops": ["encrypt", "decrypt"],
            "k": _b64url_nopad(key),
            "ext": True,
        },
        "iv": _b64(iv),
        "hashes": {"sha256": _b64(hashlib.sha256(ciphertext).digest())},
    }
    return ciphertext, file_meta


def decrypt_attachment(ciphertext: bytes, file_meta: dict[str, Any]) -> bytes:
    """Verify the sha256 then AES-256-CTR decrypt. Raises on hash mismatch (a
    tampered/corrupt blob), so callers fail closed."""
    expected = file_meta.get("hashes", {}).get("sha256")
    if expected:
        actual = _b64(hashlib.sha256(ciphertext).digest())
        # tolerate padding differences in the expected value
        if actual.rstrip("=") != str(expected).rstrip("="):
            raise ValueError("attachment sha256 mismatch — corrupt or tampered blob")
    key = _b64url_unpad_decode(file_meta["key"]["k"])
    iv = base64.b64decode(file_meta["iv"])
    dec = Cipher(algorithms.AES(key), modes.CTR(iv)).decryptor()
    return dec.update(ciphertext) + dec.finalize()


# ─── HTTP media client (gateway-host media paths, bot token) ───────────────


def media_base_from_gateway(gateway_url: str) -> str:
    """The HTTPS media base = the gateway host (wss→https), no path."""
    p = urlparse(gateway_url)
    scheme = "https" if p.scheme in ("wss", "https") else "http"
    return f"{scheme}://{p.netloc}"


@dataclass
class MediaClient:
    media_base: str
    access_token: str
    timeout: float = 60.0

    async def upload(
        self, ciphertext: bytes, content_type: str = "application/octet-stream"
    ) -> str:
        """POST ciphertext → returns the mxc:// URI."""
        return await asyncio.to_thread(self._upload, ciphertext, content_type)

    async def download(self, mxc: str) -> bytes:
        """GET ciphertext for an mxc:// URI (authenticated media, Matrix 1.11)."""
        return await asyncio.to_thread(self._download, mxc)

    async def upload_attachment(self, data: bytes, content_type: str) -> dict[str, Any]:
        """Encrypt + upload; returns the full `file` object (with `url`)."""
        ciphertext, meta = encrypt_attachment(data)
        mxc = await self.upload(ciphertext, "application/octet-stream")
        meta["url"] = mxc
        return meta

    async def download_attachment(self, file_meta: dict[str, Any]) -> bytes:
        """Download + verify + decrypt → plaintext bytes."""
        mxc = file_meta.get("url")
        if not mxc:
            raise ValueError("file object has no url")
        ciphertext = await self.download(mxc)
        return decrypt_attachment(ciphertext, file_meta)

    # ─── internals ────────────────────────────────────────────────────────

    def _upload(self, ciphertext: bytes, content_type: str) -> str:
        req = urllib.request.Request(  # noqa: S310  # our own gateway-host media endpoint
            f"{self.media_base}/_matrix/media/v3/upload",
            data=ciphertext,
            headers={"Authorization": f"Bearer {self.access_token}", "Content-Type": content_type},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310  # our own gateway-host media endpoint
            body = json.loads(resp.read().decode("utf-8"))
        content_uri: str = body["content_uri"]
        return content_uri

    def _download(self, mxc: str) -> bytes:
        server, media_id = _parse_mxc(mxc)
        url = f"{self.media_base}/_matrix/client/v1/media/download/{server}/{media_id}"
        req = urllib.request.Request(  # noqa: S310  # our own gateway-host media endpoint
            url, headers={"Authorization": f"Bearer {self.access_token}"}, method="GET"
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310  # our own gateway-host media endpoint
            data: bytes = resp.read()
        return data


def _parse_mxc(mxc: str) -> tuple[str, str]:
    if not mxc.startswith("mxc://"):
        raise ValueError(f"not an mxc uri: {mxc!r}")
    rest = mxc[len("mxc://") :]
    server, _, media_id = rest.partition("/")
    return server, media_id
