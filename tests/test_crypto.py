"""Unit tests for cryptographic primitives and PII protection."""

import pytest

from solution.crypto import decrypt_pii, encrypt_pii, hash_phone, mask_phone


def test_encrypt_decrypt_roundtrip():
    """Verify that plaintext PII is securely encrypted and decrypted."""
    plain_name = "Priya Sharma"
    plain_phone = "+919876543210"

    enc_name = encrypt_pii(plain_name)
    enc_phone = encrypt_pii(plain_phone)

    assert enc_name is not None
    assert enc_phone is not None
    assert enc_name != plain_name
    assert enc_phone != plain_phone

    # Verify decryption restores original values exactly
    assert decrypt_pii(enc_name) == plain_name
    assert decrypt_pii(enc_phone) == plain_phone


def test_authenticated_encryption_tamper_detection():
    """Verify that tampering with ciphertext triggers an authentication failure."""
    import base64

    plain = "Confidential Customer"
    armored = encrypt_pii(plain)
    assert armored is not None

    raw_bytes = bytearray(base64.b64decode(armored.encode("ascii")))
    # Flip a bit in the ciphertext / tag region
    raw_bytes[-1] ^= 0x01
    tampered_armored = base64.b64encode(raw_bytes).decode("ascii")

    # AES-GCM must detect authentication tag mismatch
    with pytest.raises(ValueError, match="PII decryption failed or authentication tag mismatch"):
        decrypt_pii(tampered_armored)


def test_phone_hashing_is_deterministic():
    """Verify HMAC blind index is deterministic for exact matching."""
    phone_a = "+919876543210"
    phone_b = "+919876543210"
    phone_c = "+919876543211"

    hash_a = hash_phone(phone_a)
    hash_b = hash_phone(phone_b)
    hash_c = hash_phone(phone_c)

    assert hash_a == hash_b
    assert hash_a != hash_c
    assert len(hash_a) == 64  # SHA-256 produces 64 hex characters


def test_phone_masking():
    """Verify phone numbers are masked as +91******XXXX for reporting."""
    assert mask_phone("+917718750036") == "+91******0036"
    assert mask_phone("91-77187-50036") == "+91******0036"
    assert mask_phone("9876543210") == "+91******3210"
    assert mask_phone("") == ""
    assert mask_phone(None) == ""
