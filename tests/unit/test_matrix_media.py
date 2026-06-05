"""Encrypted-attachment AES-CTR core + media helpers (pure, no network)."""

from __future__ import annotations

import pytest

from chat4000_hermes_plugin.matrix.media import (
    _parse_mxc,
    decrypt_attachment,
    encrypt_attachment,
    media_base_from_gateway,
)


def test_encrypt_decrypt_roundtrip():
    data = b"hello \x00\x01\x02 voice note bytes" * 1000
    ciphertext, meta = encrypt_attachment(data)
    assert ciphertext != data  # actually encrypted
    assert meta["v"] == "v2" and meta["key"]["alg"] == "A256CTR"
    assert "url" not in meta  # caller fills url after upload
    meta["url"] = "mxc://hs/abc"  # decrypt ignores url
    assert decrypt_attachment(ciphertext, meta) == data


def test_decrypt_accepts_unpadded_iv():
    """Real clients (iOS) send the EncryptedFile `iv` as UNPADDED standard base64
    (16-byte IV → 22 chars). Strict b64decode rejected that with 'Incorrect
    padding' → every inbound voice note died before reaching Hermes. Decrypt must
    accept the unpadded form. Strip the padding our encoder writes to simulate."""
    data = b"voice note bytes" * 500
    ciphertext, meta = encrypt_attachment(data)
    meta["url"] = "mxc://hs/abc"
    meta["iv"] = meta["iv"].rstrip("=")  # what a phone actually sends
    assert "=" not in meta["iv"]
    assert decrypt_attachment(ciphertext, meta) == data


def test_tamper_is_rejected():
    ciphertext, meta = encrypt_attachment(b"secret")
    tampered = bytes([ciphertext[0] ^ 0xFF]) + ciphertext[1:]
    with pytest.raises(ValueError):
        decrypt_attachment(tampered, meta)


def test_media_base_from_gateway():
    assert (
        media_base_from_gateway("wss://gateway.chat4000.com/ws") == "https://gateway.chat4000.com"
    )
    assert media_base_from_gateway("ws://localhost:8080/ws") == "http://localhost:8080"


def test_parse_mxc():
    assert _parse_mxc("mxc://chat4000.com/AbC123") == ("chat4000.com", "AbC123")
    with pytest.raises(ValueError):
        _parse_mxc("https://nope")
