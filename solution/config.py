"""Configuration settings for database, security, and paths."""

from __future__ import annotations

import os
from datetime import timedelta, timezone
from pathlib import Path

# Base Paths
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = REPO_ROOT / "data" / "ledger.db"
SCHEMAS_DIR = REPO_ROOT / "assignment-a-data" / "schemas"
DISPOSITIONS_PATH = SCHEMAS_DIR / "dispositions.csv"

# Timezone
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

# Cryptographic Keys (Can be overridden via environment variables)
# In production, these would be loaded from a secrets manager / vault.
PII_SECRET_KEY = os.environ.get(
    "PII_SECRET_KEY",
    # 32-byte key in hex format for AES-256-GCM
    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
)

PII_HASH_SALT = os.environ.get(
    "PII_HASH_SALT",
    "agentic_universe_secret_lead_salt_2026",
)


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

