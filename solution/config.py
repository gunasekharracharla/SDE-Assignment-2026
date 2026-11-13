"""Configuration settings for database, security, and paths."""

from __future__ import annotations

import logging
import os
import re
from datetime import timedelta, timezone
from pathlib import Path
from typing import Tuple

logger = logging.getLogger(__name__)

# Base Paths
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = REPO_ROOT / "data" / "ledger.db"
SCHEMAS_DIR = REPO_ROOT / "assignment-a-data" / "schemas"
DISPOSITIONS_PATH = SCHEMAS_DIR / "dispositions.csv"

# Timezone
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

# Known default development secrets (for local test & assignment evaluation)
DEFAULT_DEV_PII_SECRET_KEY = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
DEFAULT_DEV_PII_HASH_SALT = "agentic_universe_secret_lead_salt_2026"

# Legacy module-level exports for backward compatibility
PII_SECRET_KEY = os.environ.get("PII_SECRET_KEY", DEFAULT_DEV_PII_SECRET_KEY)
PII_HASH_SALT = os.environ.get("PII_HASH_SALT", DEFAULT_DEV_PII_HASH_SALT)


class CryptoConfigError(ValueError):
    """Raised when cryptographic key or salt configuration is missing or invalid."""
    pass


def get_app_env() -> str:
    """Return normalized environment mode: 'production', 'staging', 'test', or 'development'."""
    raw = os.environ.get("APP_ENV") or os.environ.get("ENVIRONMENT")
    if raw is not None:
        env = raw.strip().lower()
        if env in ("production", "prod"):
            return "production"
        if env in ("staging", "stage"):
            return "staging"
        if env in ("test", "testing"):
            return "test"
        if env in ("development", "dev"):
            return "development"
    if "PYTEST_CURRENT_TEST" in os.environ:
        return "test"
    return "development"


def validate_aes_key(raw_key: str | None, is_production: bool = False) -> bytes:
    """Validate and return 32-byte AES-256 key.

    Security rules:
    1. In production/staging, key must be explicitly set and cannot use the default dev key.
    2. Any supplied key must be exactly 64 hexadecimal characters representing 32 bytes.
    3. Weak keys (all identical characters) are rejected.
    4. Secrets are NEVER printed in error messages.
    """
    if raw_key is None:
        if is_production:
            raise CryptoConfigError(
                "Production and staging environments require PII_SECRET_KEY to be set via environment variable."
            )
        return bytes.fromhex(DEFAULT_DEV_PII_SECRET_KEY)

    key_clean = raw_key.strip()
    if not key_clean:
        raise CryptoConfigError(
            "PII_SECRET_KEY cannot be empty or whitespace."
        )

    if is_production and key_clean.lower() == DEFAULT_DEV_PII_SECRET_KEY.lower():
        raise CryptoConfigError(
            "PII_SECRET_KEY is configured with the insecure default development key in production mode."
        )

    # Format check: exactly 64 hex characters
    if len(key_clean) != 64 or not re.fullmatch(r"^[0-9a-fA-F]{64}$", key_clean):
        raise CryptoConfigError(
            f"PII_SECRET_KEY must be exactly 64 hexadecimal characters representing 32 bytes "
            f"(received {len(key_clean)} characters, non-hex or invalid format)."
        )

    # Rejection of trivial repeated keys (e.g. 0000... or ffff...)
    if len(set(key_clean.lower())) <= 2:
        raise CryptoConfigError(
            "PII_SECRET_KEY is too weak (trivial repeated character pattern detected)."
        )

    try:
        key_bytes = bytes.fromhex(key_clean)
        if len(key_bytes) != 32:
            raise CryptoConfigError(
                f"Decoded PII_SECRET_KEY length mismatch: expected 32 bytes, got {len(key_bytes)} bytes."
            )
        return key_bytes
    except ValueError:
        raise CryptoConfigError(
            "PII_SECRET_KEY failed hexadecimal decoding into 32 raw bytes."
        ) from None


def validate_hash_salt(raw_salt: str | None, is_production: bool = False) -> str:
    """Validate and return HMAC blind indexing salt.

    Security rules:
    1. In production/staging, salt must be explicitly set and cannot use the default dev salt.
    2. Salt must be non-empty and meeting minimum operational length.
    3. Secrets are NEVER printed in error messages.
    """
    if raw_salt is None:
        if is_production:
            raise CryptoConfigError(
                "Production and staging environments require PII_HASH_SALT to be set via environment variable."
            )
        return DEFAULT_DEV_PII_HASH_SALT

    salt_clean = raw_salt.strip()
    if not salt_clean:
        raise CryptoConfigError("PII_HASH_SALT cannot be empty or whitespace.")

    if is_production and salt_clean == DEFAULT_DEV_PII_HASH_SALT:
        raise CryptoConfigError(
            "PII_HASH_SALT is configured with the insecure default development salt in production mode."
        )

    if is_production:
        if len(salt_clean) < 16:
            raise CryptoConfigError(
                f"PII_HASH_SALT must be at least 16 characters in production mode (received {len(salt_clean)} characters)."
            )
        if len(set(salt_clean)) < 4:
            raise CryptoConfigError(
                "PII_HASH_SALT is too weak (insufficient character variety detected)."
            )

    return salt_clean


def validate_crypto_config(env: str | None = None) -> Tuple[bytes, str]:
    """Validate cryptographic secrets for the current or specified environment.

    Returns (aes_key_bytes, hash_salt_str).
    Raises CryptoConfigError if requirements are not met.
    """
    active_env = env or get_app_env()
    is_prod = active_env in ("production", "staging")

    key_bytes = validate_aes_key(os.environ.get("PII_SECRET_KEY"), is_production=is_prod)
    salt_str = validate_hash_salt(os.environ.get("PII_HASH_SALT"), is_production=is_prod)

    if not is_prod and (os.environ.get("PII_SECRET_KEY") is None or os.environ.get("PII_HASH_SALT") is None):
        if active_env != "test":
            logger.info("Running in %s mode with default development cryptographic keys.", active_env)

    return key_bytes, salt_str


def get_pii_secret_key() -> bytes:
    """Dynamically retrieve and validate the active AES-256 key as 32 bytes."""
    key_bytes, _ = validate_crypto_config()
    return key_bytes


def get_pii_hash_salt() -> str:
    """Dynamically retrieve and validate the active HMAC hash salt."""
    _, salt_str = validate_crypto_config()
    return salt_str


def get_db_path() -> Path:
    """Return the configured path to the SQLite database."""
    env_path = os.environ.get("LEDGER_DB_PATH")
    if env_path:
        return Path(env_path)
    return DEFAULT_DB_PATH


def compute_file_checksum(file_path: Path) -> str:
    """Compute SHA-256 checksum of a file for idempotency checking."""
    import hashlib
    sha = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            sha.update(chunk)
    return sha.hexdigest()
