"""Cryptographic primitives for PII protection and blind indexing."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from solution.config import PII_HASH_SALT, PII_SECRET_KEY


def _get_aes_key() -> bytes:
    """Decode or derive a 32-byte key for AES-256-GCM."""
    try:
        key_bytes = bytes.fromhex(PII_SECRET_KEY)
        if len(key_bytes) == 32:
            return key_bytes
    except ValueError:
        pass
    # Fallback derivation using SHA-256 if a passphrase was provided
    return hashlib.sha256(PII_SECRET_KEY.encode("utf-8")).digest()


def encrypt_pii(plaintext: str | None) -> str | None:
    """Encrypt PII using AES-256-GCM authenticated encryption.
    
    Returns base64-encoded string containing 12-byte nonce + ciphertext + 16-byte tag.
    Returns None if plaintext is None.
    """
    if plaintext is None:
        return None
    key = _get_aes_key()
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)  # Standard 96-bit nonce for AES-GCM
    data = plaintext.encode("utf-8")
    ct = aesgcm.encrypt(nonce, data, None)  # ciphertext includes 16-byte tag
    combined = nonce + ct
    return base64.b64encode(combined).decode("ascii")


def decrypt_pii(armored_text: str | None) -> str | None:
    """Decrypt authenticated AES-256-GCM ciphertext.
    
    Raises ValueError if decryption or authentication tag validation fails.
    Returns None if armored_text is None.
    """
    if armored_text is None:
        return None
    try:
        combined = base64.b64decode(armored_text.encode("ascii"))
        if len(combined) < 28:  # 12 bytes nonce + at least 16 bytes auth tag
            raise ValueError("Ciphertext payload too short")
        nonce = combined[:12]
        ct = combined[12:]
        key = _get_aes_key()
        aesgcm = AESGCM(key)
        plaintext = aesgcm.decrypt(nonce, ct, None)
        return plaintext.decode("utf-8")
    except (InvalidTag, Exception) as e:
        raise ValueError(f"PII decryption failed or authentication tag mismatch: {e}") from e


def hash_phone(canonical_phone: str | None, salt: str = PII_HASH_SALT) -> str | None:
    """Compute HMAC-SHA256 blind index for exact-match phone lookups.
    
    Provides deterministic matching across leads and events without exposing plaintext.
    """
    if canonical_phone is None:
        return None
    clean = canonical_phone.strip()
    return hmac.new(
        salt.encode("utf-8"),
        clean.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def mask_phone(canonical_phone: str | None) -> str:
    """Mask phone number for reporting: +91****** followed by last 4 digits.
    
    Example: +917718750036 -> +91******5036
    """
    if not canonical_phone:
        return ""
    digits = re.sub(r"\D", "", canonical_phone)
    if len(digits) >= 4:
        return f"+91******{digits[-4:]}"
    return f"+91******{digits}"
