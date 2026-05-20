"""Cryptographic primitives — byte-for-byte parity with Swift/Rust/TS.

The wire format of `compute_pairing_proof` and `wrap_group_key_to_joiner`
must match the other three impls exactly or pairing across implementations
silently breaks. Tests below pin every invariant the protocol depends on.
"""

from __future__ import annotations

import base64
import hashlib

import pytest

from chat4000_hermes_plugin.crypto import (
    GROUP_KEY_LEN,
    NONCE_LEN,
    PAIRING_CODE_ALPHABET,
    PAIR_WRAP_INFO,
    PAIRING_ROOM_PREFIX,
    compute_pairing_proof,
    decrypt,
    derive_group_id,
    derive_pairing_room_id,
    encrypt,
    format_group_qr_url,
    format_pairing_qr_url,
    generate_group_key,
    generate_pairing_code,
    generate_pairing_joiner_keypair,
    normalize_pairing_code,
    parse_group_key,
    unwrap_group_key_from_initiator,
    wrap_group_key_to_joiner,
)


class TestGroupKeyGeneration:
    def test_length(self):
        key = generate_group_key()
        assert len(key) == GROUP_KEY_LEN == 32

    def test_unique(self):
        # Catastrophically tiny chance of collision; if these collide we
        # have a much bigger problem (OS CSPRNG is broken).
        keys = {generate_group_key() for _ in range(50)}
        assert len(keys) == 50

    def test_derive_group_id_is_sha256_lowercase_hex(self):
        key = bytes(range(32))
        expected = hashlib.sha256(key).hexdigest()
        assert derive_group_id(key) == expected
        assert derive_group_id(key) == expected.lower()
        assert len(derive_group_id(key)) == 64

    def test_derive_group_id_deterministic(self):
        key = b"\x42" * 32
        assert derive_group_id(key) == derive_group_id(key)

    def test_derive_group_id_changes_with_key(self):
        assert derive_group_id(b"\x00" * 32) != derive_group_id(b"\x01" * 32)


class TestEncryption:
    def test_encrypt_decrypt_roundtrip(self):
        key = generate_group_key()
        plaintext = b"hello chat4000"
        nonce_b64, ct_b64 = encrypt(plaintext, key)
        assert decrypt(nonce_b64, ct_b64, key) == plaintext

    def test_encrypt_nonce_is_24_bytes(self):
        key = generate_group_key()
        nonce_b64, _ = encrypt(b"x", key)
        assert len(base64.b64decode(nonce_b64)) == NONCE_LEN == 24

    def test_encrypt_nonce_unique_per_call(self):
        """Nonce reuse with the same key would break AEAD security."""
        key = generate_group_key()
        nonces = {encrypt(b"x", key)[0] for _ in range(20)}
        assert len(nonces) == 20

    def test_encrypt_empty_payload(self):
        key = generate_group_key()
        nonce, ct = encrypt(b"", key)
        assert decrypt(nonce, ct, key) == b""

    def test_encrypt_64kb_payload(self):
        """Relay's max_message_size is 64KB. Round-trip the boundary."""
        key = generate_group_key()
        plaintext = b"A" * (64 * 1024)
        nonce, ct = encrypt(plaintext, key)
        assert decrypt(nonce, ct, key) == plaintext

    def test_decrypt_wrong_key_returns_none(self):
        nonce, ct = encrypt(b"secret", generate_group_key())
        assert decrypt(nonce, ct, generate_group_key()) is None

    def test_decrypt_corrupted_ciphertext_returns_none(self):
        key = generate_group_key()
        nonce, ct = encrypt(b"important", key)
        ct_bytes = bytearray(base64.b64decode(ct))
        ct_bytes[0] ^= 0xFF
        corrupted = base64.b64encode(bytes(ct_bytes)).decode("ascii")
        assert decrypt(nonce, corrupted, key) is None

    def test_decrypt_wrong_nonce_returns_none(self):
        key = generate_group_key()
        _, ct = encrypt(b"x", key)
        bad_nonce = base64.b64encode(b"\x00" * 24).decode("ascii")
        assert decrypt(bad_nonce, ct, key) is None

    def test_decrypt_invalid_base64_returns_none(self):
        key = generate_group_key()
        assert decrypt("not-valid-base64!!!", "alsoBad", key) is None

    def test_decrypt_wrong_nonce_length_returns_none(self):
        key = generate_group_key()
        _, ct = encrypt(b"x", key)
        short_nonce = base64.b64encode(b"\x00" * 12).decode("ascii")
        assert decrypt(short_nonce, ct, key) is None

    def test_encrypt_rejects_wrong_key_length(self):
        with pytest.raises(ValueError):
            encrypt(b"x", b"\x00" * 16)

    def test_decrypt_wrong_key_length_returns_none(self):
        nonce, ct = encrypt(b"x", generate_group_key())
        assert decrypt(nonce, ct, b"\x00" * 16) is None


class TestParseGroupKey:
    def test_hex_64_chars(self):
        key = bytes(range(32))
        assert parse_group_key(key.hex()) == key

    def test_hex_uppercase(self):
        key = bytes(range(32))
        assert parse_group_key(key.hex().upper()) == key

    def test_base64(self):
        key = bytes(range(32))
        b64 = base64.b64encode(key).decode("ascii")
        assert parse_group_key(b64) == key

    def test_base64_no_padding(self):
        key = bytes(range(32))
        b64 = base64.b64encode(key).decode("ascii").rstrip("=")
        assert parse_group_key(b64) == key

    def test_base64url(self):
        key = bytes(range(32))
        b64url = base64.urlsafe_b64encode(key).rstrip(b"=").decode("ascii")
        assert parse_group_key(b64url) == key

    def test_wrong_length_raises(self):
        # 16-byte hex doesn't match the 64-char hex pattern and decodes as
        # base64 to 12 bytes — should reject.
        with pytest.raises(ValueError):
            parse_group_key("abcd")

    def test_garbage_raises(self):
        with pytest.raises(ValueError):
            parse_group_key("!!!not a key!!!")


class TestPairingCode:
    def test_alphabet_excludes_ambiguous_chars(self):
        """Same alphabet as Swift/Rust/TS impls — 28 chars excluding
        0,1,5,I,L,O,S."""
        for c in "0115ILOS":
            assert c not in PAIRING_CODE_ALPHABET
        assert len(PAIRING_CODE_ALPHABET) == 28

    def test_generate_format(self):
        code = generate_pairing_code()
        assert len(code) == 9  # 4 + dash + 4
        assert code[4] == "-"
        for c in code.replace("-", ""):
            assert c in PAIRING_CODE_ALPHABET

    def test_generate_random(self):
        codes = {generate_pairing_code() for _ in range(20)}
        assert len(codes) == 20

    def test_normalize_strips_dashes_and_whitespace(self):
        assert normalize_pairing_code("abcd-efgh") == "ABCDEFGH"
        assert normalize_pairing_code(" ab cd-ef gh ") == "ABCDEFGH"
        assert normalize_pairing_code("ABCD\nEFGH") == "ABCDEFGH"

    def test_normalize_filters_invalid_chars(self):
        # I, L, O, S, 0, 1, 5 all banned. Allowed digits: 2,3,4,6,7,8,9.
        assert normalize_pairing_code("ABCDILOS") == "ABCD"
        assert normalize_pairing_code("0123456789") == "2346789"

    def test_normalize_uppercases(self):
        assert normalize_pairing_code("abcdefgh") == "ABCDEFGH"

    def test_derive_pairing_room_id_format(self):
        room = derive_pairing_room_id("ABCD-2346")
        assert len(room) == 64
        all(c in "0123456789abcdef" for c in room)

    def test_derive_pairing_room_id_uses_prefix(self):
        # Manually compute the expected value to lock in byte-for-byte
        # parity with Swift/Rust/TS.
        normalized = "ABCD2346"
        h = hashlib.sha256()
        h.update(PAIRING_ROOM_PREFIX)
        h.update(normalized.encode("utf-8"))
        assert derive_pairing_room_id("ABCD-2346") == h.hexdigest()

    def test_derive_pairing_room_id_normalizes_input(self):
        assert derive_pairing_room_id("abcd-2346") == derive_pairing_room_id("ABCD2346")
        assert derive_pairing_room_id(" abcd 2346 ") == derive_pairing_room_id("ABCD2346")


class TestPairingProof:
    """The proof MUST match the Swift/Rust/TS impls exactly. Each separator
    is a single 0x00 byte, not a delimiter character. Side labels are
    literal ASCII "A" / "B"."""

    def test_proof_format_byte_for_byte(self):
        code = "ABCD2346"
        salt = b"\x10" * 32
        pub = b"\x20" * 32
        h = hashlib.sha256()
        h.update(code.encode("utf-8"))
        h.update(b"\x00")
        h.update(salt)
        h.update(b"\x00")
        h.update(pub)
        h.update(b"\x00")
        h.update(b"A")
        expected = base64.b64encode(h.digest()).decode("ascii")
        actual = compute_pairing_proof(
            code,
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(pub).decode("ascii"),
            "A",
        )
        assert actual == expected

    def test_proof_a_and_b_differ(self):
        salt = base64.b64encode(b"\x10" * 32).decode("ascii")
        pub = base64.b64encode(b"\x20" * 32).decode("ascii")
        a = compute_pairing_proof("ABCD2346", salt, pub, "A")
        b = compute_pairing_proof("ABCD2346", salt, pub, "B")
        assert a != b

    def test_proof_deterministic(self):
        salt = base64.b64encode(b"\x42" * 32).decode("ascii")
        pub = base64.b64encode(b"\x99" * 32).decode("ascii")
        p1 = compute_pairing_proof("CODE2346", salt, pub, "A")
        p2 = compute_pairing_proof("CODE2346", salt, pub, "A")
        assert p1 == p2

    def test_proof_changes_with_each_input(self):
        s = base64.b64encode(b"\x10" * 32).decode("ascii")
        p = base64.b64encode(b"\x20" * 32).decode("ascii")
        base = compute_pairing_proof("ABCD2346", s, p, "A")
        assert compute_pairing_proof("ABCD2347", s, p, "A") != base
        diff_salt = base64.b64encode(b"\x11" * 32).decode("ascii")
        assert compute_pairing_proof("ABCD2346", diff_salt, p, "A") != base
        diff_pub = base64.b64encode(b"\x21" * 32).decode("ascii")
        assert compute_pairing_proof("ABCD2346", s, diff_pub, "A") != base

    def test_proof_requires_already_normalized_code(self):
        # The impl takes a `normalized_code` parameter — caller is
        # responsible for normalizing first. Passing the dashed form
        # produces a different hash because the dash is hashed verbatim.
        s = base64.b64encode(b"\x10" * 32).decode("ascii")
        p = base64.b64encode(b"\x20" * 32).decode("ascii")
        assert (
            compute_pairing_proof("ABCD-2346", s, p, "A")
            != compute_pairing_proof("ABCD2346", s, p, "A")
        )


class TestWrappedGroupKey:
    def test_roundtrip(self):
        joiner = generate_pairing_joiner_keypair()
        group_key = generate_group_key()
        wrapped = wrap_group_key_to_joiner(joiner.public_key_base64, group_key)
        unwrapped = unwrap_group_key_from_initiator(wrapped, joiner.private_key)
        assert unwrapped == group_key

    def test_wrap_rejects_wrong_key_length(self):
        joiner = generate_pairing_joiner_keypair()
        with pytest.raises(ValueError):
            wrap_group_key_to_joiner(joiner.public_key_base64, b"\x00" * 16)

    def test_unwrap_wrong_private_key_returns_none(self):
        joiner1 = generate_pairing_joiner_keypair()
        joiner2 = generate_pairing_joiner_keypair()
        wrapped = wrap_group_key_to_joiner(joiner1.public_key_base64, generate_group_key())
        # Other joiner's privkey should NOT decrypt.
        assert unwrap_group_key_from_initiator(wrapped, joiner2.private_key) is None

    def test_unwrap_corrupted_returns_none(self):
        joiner = generate_pairing_joiner_keypair()
        wrapped = wrap_group_key_to_joiner(joiner.public_key_base64, generate_group_key())
        # Tamper with ephemeral pub.
        wrapped.ephemeral_pub = base64.b64encode(b"\xff" * 32).decode("ascii")
        assert unwrap_group_key_from_initiator(wrapped, joiner.private_key) is None

    def test_wrap_includes_pair_wrap_info(self):
        # The wrap-key derivation uses sha256(shared || "chat4000-pair-wrap-v1").
        # If the constant ever drifts, cross-impl pairing breaks silently.
        # Pin the constant byte string here.
        assert PAIR_WRAP_INFO == b"chat4000-pair-wrap-v1"

    def test_joiner_keypair_has_32_byte_pub(self):
        keypair = generate_pairing_joiner_keypair()
        assert len(keypair.public_key) == 32


class TestQrUrls:
    def test_group_qr_url(self):
        key = bytes(range(32))
        url = format_group_qr_url(key)
        assert url.startswith("chat4000://pair/")
        # The encoded key should be base64url with no padding.
        encoded = url.split("/")[-1]
        assert "=" not in encoded

    def test_pairing_qr_url(self):
        url = format_pairing_qr_url("ABCD-2346")
        assert url == "chat4000://pair?code=ABCD-2346"
