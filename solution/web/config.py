"""Configuration and security utilities for the web application."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Set, Tuple

from solution.config import REPO_ROOT

# Upload Limits & Security
MAX_UPLOAD_BYTES: int = 50 * 1024 * 1024  # 50 MB
STREAM_CHUNK_SIZE: int = 64 * 1024  # 64 KB streaming buffer

# Allowed Workflows & Files
ALLOWED_WORKFLOWS: Tuple[str, ...] = ("motor_renewal", "policy_feedback")
ALLOWED_EXTENSIONS_LEADS: Set[str] = {".csv"}
ALLOWED_EXTENSIONS_EVENTS: Set[str] = {".jsonl", ".json"}

ALLOWED_REPORT_FILES: Set[str] = {
    "batch_summary.csv",
    "daily_overview.csv",
    "lead_report.csv",
    "rejections.csv",
    "data_quality.json",
}

# Directories
DEFAULT_UPLOADS_DIR: Path = REPO_ROOT / "data" / "uploads"
DEFAULT_REPORTS_DIR: Path = REPO_ROOT / "reports"


def sanitize_filename(filename: str | None) -> str:
    """Sanitize uploaded filename to prevent directory traversal and filesystem attacks."""
    if not filename:
        return "unnamed_upload"
    # Extract only the base name (no paths)
    base = Path(filename).name
    # Strip any dangerous characters, keep only alphanumerics, dots, hyphens, underscores
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]", "_", base)
    # Prevent hidden files or relative dots
    cleaned = cleaned.lstrip(".")
    return cleaned or "upload"


def validate_workflow_id(workflow_id: str) -> bool:
    """Validate that the workflow_id is among supported workflows."""
    return workflow_id in ALLOWED_WORKFLOWS


class FileSizeLimitExceeded(Exception):
    """Raised when an uploaded file exceeds the streaming limit."""
    pass


async def save_stream_safely(
    source_file,
    destination_path: Path,
    max_bytes: int = MAX_UPLOAD_BYTES,
    chunk_size: int = STREAM_CHUNK_SIZE,
) -> int:
    """Stream incoming upload directly to disk while strictly enforcing file size limits.
    
    Prevents memory exhaustion attacks by writing in small chunks and terminating
    immediately if max_bytes is exceeded.
    """
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    bytes_written = 0

    try:
        with open(destination_path, "wb") as dest:
            while True:
                chunk = await source_file.read(chunk_size)
                if not chunk:
                    break
                bytes_written += len(chunk)
                if bytes_written > max_bytes:
                    raise FileSizeLimitExceeded(
                        f"File size exceeds maximum allowed limit of {max_bytes // (1024 * 1024)} MB."
                    )
                dest.write(chunk)
        return bytes_written
    except Exception:
        # Clean up partial file on failure to prevent orphaned partial files
        if destination_path.exists():
            try:
                destination_path.unlink()
            except OSError:
                pass
        raise
