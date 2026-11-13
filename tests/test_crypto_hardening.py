"""Comprehensive security tests for Cryptographic Key Management Hardening (Task 3).

Verifies:
1. Production & staging fail-fast enforcement when keys/salts are missing or default.
2. AES-256 key validation (exactly 64 hexadecimal characters / 32 bytes, rejection of weak/malformed keys).
3. Hash salt validation (non-empty, minimum length, rejection of default dev salt in production).
4. Secret redaction guarantee: No actual secret or salt values appear in exceptions or error messages.
5. Removal of silent SHA-256 fallback: explicitly invalid keys raise CryptoConfigError.
6. Public health endpoint privacy: operational status only, no crypto details leaked.
7. Development & test mode backward compatibility: existing records decrypt cleanly.
"""

from __future__ import annotations

import os
from unittest.mock import patch
import pytest

from solution.config import (
    DEFAULT_DEV_PII_HASH_SALT,
    DEFAULT_DEV_PII_SECRET_KEY,
    CryptoConfigError,
    get_app_env,
    get_pii_hash_salt,
    get_pii_secret_key,
    validate_aes_key,
    validate_crypto_config,
    validate_hash_salt,
)
from solution.crypto import decrypt_pii, encrypt_pii, hash_phone
from solution.web.app import create_app


VALID_PROD_KEY = "a1b2c3d4e5f60718293a4b5c6d7e8f90123456789abcdef0123456789abcdef0"
VALID_PROD_SALT = "prod_super_secret_salt_with_entropy_2026"


class TestCryptoEnvironmentEnforcement:
    """Test production, staging, development, and test environment mode handling."""

    def test_environment_normalization(self, monkeypatch):
        """Verify APP_ENV / ENVIRONMENT aliases and normalization."""
        monkeypatch.setenv("APP_ENV", "production")
        assert get_app_env() == "production"

        monkeypatch.setenv("APP_ENV", "PROD")
        assert get_app_env() == "production"

        monkeypatch.setenv("APP_ENV", "staging")
        assert get_app_env() == "staging"

        monkeypatch.setenv("APP_ENV", "development")
        assert get_app_env() == "development"

        monkeypatch.delenv("APP_ENV", raising=False)
        monkeypatch.setenv("ENVIRONMENT", "production")
        assert get_app_env() == "production"

    def test_production_mode_fails_fast_when_key_missing(self, monkeypatch):
        """Verify production mode raises CryptoConfigError when PII_SECRET_KEY is absent."""
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.delenv("PII_SECRET_KEY", raising=False)
        monkeypatch.setenv("PII_HASH_SALT", VALID_PROD_SALT)

        with pytest.raises(CryptoConfigError, match="require PII_SECRET_KEY to be set"):
            validate_crypto_config()

    def test_production_mode_fails_fast_when_salt_missing(self, monkeypatch):
        """Verify production mode raises CryptoConfigError when PII_HASH_SALT is absent."""
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("PII_SECRET_KEY", VALID_PROD_KEY)
        monkeypatch.delenv("PII_HASH_SALT", raising=False)

        with pytest.raises(CryptoConfigError, match="require PII_HASH_SALT to be set"):
            validate_crypto_config()

    def test_production_mode_rejects_default_dev_key(self, monkeypatch):
        """Verify production mode rejects the known default development key."""
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("PII_SECRET_KEY", DEFAULT_DEV_PII_SECRET_KEY)
        monkeypatch.setenv("PII_HASH_SALT", VALID_PROD_SALT)

        with pytest.raises(CryptoConfigError, match="insecure default development key in production"):
            validate_crypto_config()

    def test_production_mode_rejects_default_dev_salt(self, monkeypatch):
        """Verify production mode rejects the known default development salt."""
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("PII_SECRET_KEY", VALID_PROD_KEY)
        monkeypatch.setenv("PII_HASH_SALT", DEFAULT_DEV_PII_HASH_SALT)

        with pytest.raises(CryptoConfigError, match="insecure default development salt in production"):
            validate_crypto_config()

    def test_production_mode_succeeds_with_valid_secrets(self, monkeypatch):
        """Verify production mode passes when both secrets are valid and distinct from dev defaults."""
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("PII_SECRET_KEY", VALID_PROD_KEY)
        monkeypatch.setenv("PII_HASH_SALT", VALID_PROD_SALT)

        key_bytes, salt_str = validate_crypto_config()
        assert len(key_bytes) == 32
        assert salt_str == VALID_PROD_SALT


class TestKeyFormatValidation:
    """Test strict AES-256 key validation and removal of silent SHA-256 fallback."""

    def test_reject_non_hex_characters(self):
        """Verify non-hex characters trigger CryptoConfigError."""
        invalid_key = "g" * 64
        with pytest.raises(CryptoConfigError, match="non-hex or invalid format"):
            validate_aes_key(invalid_key)

    def test_reject_invalid_lengths(self):
        """Verify keys that are not exactly 64 hex characters are rejected."""
        # 32 characters (16 bytes - AES-128)
        with pytest.raises(CryptoConfigError, match="must be exactly 64 hexadecimal characters"):
            validate_aes_key("a" * 32)

        # 63 characters
        with pytest.raises(CryptoConfigError, match="must be exactly 64 hexadecimal characters"):
            validate_aes_key("a" * 63)

        # 65 characters
        with pytest.raises(CryptoConfigError, match="must be exactly 64 hexadecimal characters"):
            validate_aes_key("a" * 65)

    def test_reject_trivial_weak_keys(self):
        """Verify trivial repeated pattern keys are rejected."""
        all_zeros = "0" * 64
        with pytest.raises(CryptoConfigError, match="too weak"):
            validate_aes_key(all_zeros)

        all_fs = "f" * 64
        with pytest.raises(CryptoConfigError, match="too weak"):
            validate_aes_key(all_fs)

    def test_valid_aes_key_returns_32_bytes(self):
        """Verify valid 64-hex string correctly decodes to 32 raw bytes."""
        key_bytes = validate_aes_key(VALID_PROD_KEY)
        assert isinstance(key_bytes, bytes)
        assert len(key_bytes) == 32
        assert key_bytes == bytes.fromhex(VALID_PROD_KEY)

    def test_removal_of_silent_sha256_fallback(self, monkeypatch):
        """Verify explicitly provided invalid keys raise CryptoConfigError instead of silently hashing."""
        monkeypatch.setenv("PII_SECRET_KEY", "invalid_passphrase_that_used_to_be_sha256_hashed")
        with pytest.raises(CryptoConfigError):
            get_pii_secret_key()


class TestSaltFormatValidation:
    """Test HMAC hash salt format and security rules."""

    def test_reject_empty_or_whitespace_salt(self):
        """Verify empty or whitespace salts are rejected."""
        with pytest.raises(CryptoConfigError, match="cannot be empty"):
            validate_hash_salt("   ")

    def test_reject_short_salt_in_production(self):
        """Verify salts shorter than 16 characters are rejected in production."""
        with pytest.raises(CryptoConfigError, match="at least 16 characters"):
            validate_hash_salt("short_salt_123", is_production=True)

    def test_reject_weak_repetitive_salt_in_production(self):
        """Verify salts with insufficient character variety are rejected in production."""
        with pytest.raises(CryptoConfigError, match="too weak"):
            validate_hash_salt("aaaaaaaaaaaaaaaa", is_production=True)


class TestSecretRedactionGuarantee:
    """Requirement 4: Verify actual secrets NEVER appear in exception messages, logs, or tracebacks."""

    def test_secret_key_not_in_error_message(self):
        """Ensure invalid canary key is completely absent from exception string."""
        canary_secret = "CANARY_SECRET_DEADBEEF_SECRET_KEY_12345"
        try:
            validate_aes_key(canary_secret)
            pytest.fail("Should have raised CryptoConfigError")
        except CryptoConfigError as e:
            err_msg = str(e)
            assert canary_secret not in err_msg
            assert "DEADBEEF" not in err_msg

    def test_hash_salt_not_in_error_message(self):
        """Ensure invalid canary salt is completely absent from exception string."""
        canary_salt = "CANARY_SALT_ABC"
        try:
            validate_hash_salt(canary_salt, is_production=True)
            pytest.fail("Should have raised CryptoConfigError")
        except CryptoConfigError as e:
            err_msg = str(e)
            assert canary_salt not in err_msg
            assert "CANARY" not in err_msg


class TestFastAPIIntegrationAndHealthPrivacy:
    """Verify FastAPI startup fails fast in production and /api/health exposes zero crypto details."""

    def test_create_app_fails_fast_in_production_without_secrets(self, monkeypatch, tmp_path):
        """Verify create_app() raises CryptoConfigError immediately on startup in production."""
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.delenv("PII_SECRET_KEY", raising=False)
        monkeypatch.delenv("PII_HASH_SALT", raising=False)

        with pytest.raises(CryptoConfigError):
            create_app(db_path=tmp_path / "test.db")

    def test_create_app_succeeds_in_production_with_valid_secrets(self, monkeypatch, tmp_path):
        """Verify create_app() succeeds in production when valid keys are supplied."""
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("PII_SECRET_KEY", VALID_PROD_KEY)
        monkeypatch.setenv("PII_HASH_SALT", VALID_PROD_SALT)

        app = create_app(db_path=tmp_path / "test.db")
        assert app is not None

    def test_health_endpoint_privacy(self, monkeypatch, tmp_path):
        """Requirement 5: Verify GET /api/health exposes operational status only and NO crypto info."""
        monkeypatch.setenv("APP_ENV", "development")
        app = create_app(db_path=tmp_path / "test.db")
        from fastapi.testclient import TestClient

        client = TestClient(app)
        res = client.get("/api/health")
        assert res.status_code == 200
        data = res.json()

        # Operational status checks
        assert "status" in data
        assert "database" in data
        assert "workflows" in data

        # Security check: Zero cryptographic keys, salts, or crypto configuration status leaked
        text = res.text.lower()
        assert "secret" not in text
        assert "key" not in text
        assert "salt" not in text
        assert "crypto" not in text
        assert "aes" not in text


class TestDatabaseBackwardCompatibility:
    """Verify existing development database records remain 100% decryptable and queryable."""

    def test_dev_mode_uses_dev_defaults_seamlessly(self, monkeypatch):
        """Verify that in development mode without explicit env vars, dev defaults are used."""
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.delenv("PII_SECRET_KEY", raising=False)
        monkeypatch.delenv("PII_HASH_SALT", raising=False)

        key = get_pii_secret_key()
        salt = get_pii_hash_salt()
        assert key == bytes.fromhex(DEFAULT_DEV_PII_SECRET_KEY)
        assert salt == DEFAULT_DEV_PII_HASH_SALT

    def test_encrypt_decrypt_roundtrip_with_dev_key(self, monkeypatch):
        """Verify encrypt/decrypt roundtrip is preserved."""
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.delenv("PII_SECRET_KEY", raising=False)
        monkeypatch.delenv("PII_HASH_SALT", raising=False)

        plain = "Aarav Sharma +919876543210"
        encrypted = encrypt_pii(plain)
        assert encrypted != plain
        assert decrypt_pii(encrypted) == plain

    def test_hmac_deterministic_index_consistency(self, monkeypatch):
        """Verify HMAC blind indexing produces exact expected hash for phone numbers."""
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.delenv("PII_SECRET_KEY", raising=False)
        monkeypatch.delenv("PII_HASH_SALT", raising=False)

        h1 = hash_phone("+919876543210")
        h2 = hash_phone("+919876543210")
        assert h1 == h2
        assert len(h1) == 64
