"""Cryptographic primitives for chat4000 — XChaCha20-Poly1305 + X25519 pairing.

Port of clawconnect-plugin/src/crypto.ts. Identical wire format so messages
encrypted in TypeScript/Rust/Swift round-trip with Python.

Uses PyNaCl (libsodium bindings) — same library that Swift's swift-sodium
binds to and that the TypeScript plugin uses via @noble/ciphers's
xchacha20poly1305 (which itself implements the IETF spec libsodium uses).

Group key model (per chat4000 protocol §3):
  - 32-byte shared secret = group key
  - group_id = lowercase_hex(sha256(group_key))
  - inner messages encrypted with XChaCha20-Poly1305 + random 24-byte nonce
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import string
from dataclasses import dataclass

import nacl.bindings as _nacl_bind
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)

from .protocol_types import RelayWrappedKeyPayload

# ─── Constants (must match TS impl byte-for-byte) ──────────────────────────

GROUP_KEY_LEN = 32
NONCE_LEN = 24  # XChaCha20-Poly1305 nonce
PAIRING_ROOM_PREFIX = b"pairing-v1:"
PAIR_WRAP_INFO = b"chat4000-pair-wrap-v1"
# Alphabet excludes 0, 1, I, L, O, S, 5 — visually disambiguated for humans.
# Identical to the Swift/Rust/TS impls so pairing codes are interchangeable.
PAIRING_CODE_ALPHABET = "ABCDEFGHJKMNPRTUVWXYZ2346789"


# ─── Public API ────────────────────────────────────────────────────────────


def generate_group_key() -> bytes:
    """Generate a fresh 32-byte group key from the OS CSPRNG."""
    return secrets.token_bytes(GROUP_KEY_LEN)


def derive_group_id(group_key: bytes) -> str:
    """group_id = lowercase_hex(sha256(group_key))."""
    return hashlib.sha256(group_key).hexdigest()


def encrypt(plaintext: bytes, key: bytes) -> tuple[str, str]:
    """XChaCha20-Poly1305 encrypt. Returns (nonce_b64, ciphertext_b64).
    Uses a fresh random 24-byte nonce per call — nonce reuse with the same
    key would break the AEAD security guarantee."""
    if len(key) != GROUP_KEY_LEN:
        raise ValueError(f"key must be {GROUP_KEY_LEN} bytes, got {len(key)}")
    nonce = secrets.token_bytes(NONCE_LEN)
    ciphertext = _nacl_bind.crypto_aead_xchacha20poly1305_ietf_encrypt(plaintext, None, nonce, key)
    return (
        base64.b64encode(nonce).decode("ascii"),
        base64.b64encode(ciphertext).decode("ascii"),
    )


def decrypt(nonce_b64: str, ciphertext_b64: str, key: bytes) -> bytes | None:
    """XChaCha20-Poly1305 decrypt. Returns plaintext bytes, or None on auth
    failure (wrong key, tampered ciphertext, wrong nonce)."""
    try:
        nonce = base64.b64decode(nonce_b64)
        ciphertext = base64.b64decode(ciphertext_b64)
    except Exception:
        return None
    if len(nonce) != NONCE_LEN or len(key) != GROUP_KEY_LEN:
        return None
    try:
        return _nacl_bind.crypto_aead_xchacha20poly1305_ietf_decrypt(ciphertext, None, nonce, key)
    except Exception:
        return None


def parse_group_key(input_str: str) -> bytes:
    """Accept either hex (64 chars) or base64url/base64 (32 bytes decoded)."""
    s = input_str.strip()
    if len(s) == 64 and all(c in string.hexdigits for c in s):
        return bytes.fromhex(s)
    # base64 / base64url
    try:
        padded = s + "=" * (-len(s) % 4)
        # tolerate base64url (-/_) by translating to standard alphabet
        normalized = padded.replace("-", "+").replace("_", "/")
        decoded = base64.b64decode(normalized)
        if len(decoded) != GROUP_KEY_LEN:
            raise ValueError(f"group key must decode to {GROUP_KEY_LEN} bytes, got {len(decoded)}")
        return decoded
    except Exception as e:
        raise ValueError(f"could not parse group key: {e}") from e


# ─── Pairing primitives ────────────────────────────────────────────────────


def normalize_pairing_code(raw: str) -> str:
    """Strip dashes/whitespace, uppercase, filter to pairing alphabet."""
    upper = "".join(ch.upper() for ch in raw if not ch.isspace() and ch != "-")
    return "".join(ch for ch in upper if ch in PAIRING_CODE_ALPHABET)


def generate_pairing_code() -> str:
    """Generate an 8-char pairing code, formatted as XXXX-YYYY for humans."""
    raw_bytes = secrets.token_bytes(8)
    alphabet_len = len(PAIRING_CODE_ALPHABET)
    chars = [PAIRING_CODE_ALPHABET[b % alphabet_len] for b in raw_bytes]
    return f"{''.join(chars[:4])}-{''.join(chars[4:])}"


def derive_pairing_room_id(code: str) -> str:
    """room_id = lowercase_hex(sha256("pairing-v1:" || normalized_code))."""
    normalized = normalize_pairing_code(code)
    h = hashlib.sha256()
    h.update(PAIRING_ROOM_PREFIX)
    h.update(normalized.encode("utf-8"))
    return h.hexdigest()


def compute_pairing_proof(normalized_code: str, a_salt_b64: str, b_pub_b64: str, side: str) -> str:
    """proof = sha256(code || 0x00 || a_salt || 0x00 || b_pub || 0x00 || side)
    base64-encoded. Matches the TS/Swift/Rust impls exactly (each separator
    is a single 0x00 byte, not a delimiter character)."""
    a_salt = base64.b64decode(a_salt_b64)
    b_pub = base64.b64decode(b_pub_b64)
    h = hashlib.sha256()
    h.update(normalized_code.encode("utf-8"))
    h.update(b"\x00")
    h.update(a_salt)
    h.update(b"\x00")
    h.update(b_pub)
    h.update(b"\x00")
    h.update(side.encode("utf-8"))
    return base64.b64encode(h.digest()).decode("ascii")


# ─── X25519 wrap/unwrap for transferring the group key during pairing ─────


@dataclass
class JoinerKeypair:
    private_key: X25519PrivateKey
    public_key: bytes  # raw 32-byte public key
    public_key_base64: str


def generate_pairing_joiner_keypair() -> JoinerKeypair:
    """Fresh X25519 keypair, never persisted. Joiner sends the public key
    to the initiator; the wrapped group key comes back encrypted to it."""
    priv = X25519PrivateKey.generate()
    pub_raw = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return JoinerKeypair(
        private_key=priv,
        public_key=pub_raw,
        public_key_base64=base64.b64encode(pub_raw).decode("ascii"),
    )


def _derive_wrap_key(shared_secret: bytes) -> bytes:
    """wrap_key = sha256(ecdh_shared_secret || PAIR_WRAP_INFO)."""
    h = hashlib.sha256()
    h.update(shared_secret)
    h.update(PAIR_WRAP_INFO)
    return h.digest()


def wrap_group_key_to_joiner(
    recipient_public_key_b64: str, group_key: bytes
) -> RelayWrappedKeyPayload:
    """Initiator-side: encrypt group_key to the joiner's public key.

    Generates a fresh ephemeral X25519 keypair, does ECDH with the joiner's
    pubkey, derives a wrap key via sha256(shared || info), encrypts the
    32-byte group key with XChaCha20-Poly1305, and returns the wrapped
    payload (ephemeral pubkey + nonce + ciphertext)."""
    if len(group_key) != GROUP_KEY_LEN:
        raise ValueError(f"group_key must be {GROUP_KEY_LEN} bytes")
    recipient_pub = X25519PublicKey.from_public_bytes(base64.b64decode(recipient_public_key_b64))
    ephemeral_priv = X25519PrivateKey.generate()
    ephemeral_pub_raw = ephemeral_priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    shared = ephemeral_priv.exchange(recipient_pub)
    wrap_key = _derive_wrap_key(shared)
    nonce_b64, ciphertext_b64 = encrypt(group_key, wrap_key)
    return RelayWrappedKeyPayload(
        ephemeral_pub=base64.b64encode(ephemeral_pub_raw).decode("ascii"),
        nonce=nonce_b64,
        ciphertext=ciphertext_b64,
    )


def unwrap_group_key_from_initiator(
    wrapped: RelayWrappedKeyPayload, joiner_private_key: X25519PrivateKey
) -> bytes | None:
    """Joiner-side: decrypt the wrapped group key.

    Returns None on any failure — used as a security signal (don't leak
    why decryption failed) rather than raising. Caller surfaces a generic
    'pairing failed' to the user."""
    try:
        initiator_pub = X25519PublicKey.from_public_bytes(base64.b64decode(wrapped.ephemeral_pub))
        shared = joiner_private_key.exchange(initiator_pub)
        wrap_key = _derive_wrap_key(shared)
        plaintext = decrypt(wrapped.nonce, wrapped.ciphertext, wrap_key)
        if plaintext is None or len(plaintext) != GROUP_KEY_LEN:
            return None
        return plaintext
    except Exception:
        return None


# ─── QR / URI helpers ──────────────────────────────────────────────────────


def format_group_qr_url(group_key: bytes) -> str:
    """`chat4000://pair/<base64url-key>` for legacy direct-key QR codes.
    The modern flow uses pairing codes instead; this is retained for
    backwards compatibility with old QR-bearing devices."""
    b64url = base64.urlsafe_b64encode(group_key).rstrip(b"=").decode("ascii")
    return f"chat4000://pair/{b64url}"


def format_pairing_qr_url(code: str) -> str:
    """`chat4000://pair?code=XXXX-YYYY` — the format Swift/CLI clients scan."""
    return f"chat4000://pair?code={code}"
