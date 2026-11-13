"""Report query and secure download routes."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse

from solution.db import get_db_connection, init_db
from solution.reporting import generate_all_reports
from solution.web.config import ALLOWED_REPORT_FILES

router = APIRouter()


@router.get("/status", summary="Get status and metadata for all five official report files")
async def get_reports_status(request: Request) -> Dict[str, Any]:
    """Return file presence, size, row counts, and modification times for all five reports."""
    reports_dir: Path = getattr(request.app.state, "reports_dir", Path("reports"))
    db_path: Path = getattr(request.app.state, "db_path", Path("data/ledger.db"))

    # Ensure reports exist if database is initialized
    missing = any(not (reports_dir / fn).exists() for fn in ALLOWED_REPORT_FILES)
    if missing:
        try:
            with get_db_connection(db_path) as conn:
                init_db(conn)
                generate_all_reports(conn, reports_dir)
        except Exception:
            pass

    files_info: List[Dict[str, Any]] = []

    for filename in sorted(ALLOWED_REPORT_FILES):
        file_path = reports_dir / filename
        exists = file_path.exists()
        size_bytes = file_path.stat().st_size if exists else 0
        mtime = (
            datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc).isoformat()
            if exists
            else None
        )

        row_count = 0
        if exists:
            try:
                if filename.endswith(".csv"):
                    with open(file_path, "r", encoding="utf-8") as f:
                        row_count = sum(1 for _ in csv.reader(f)) - 1  # Exclude header
                        if row_count < 0:
                            row_count = 0
                elif filename.endswith(".json"):
                    row_count = 1  # 1 JSON document
            except Exception:
                row_count = 0

        files_info.append({
            "filename": filename,
            "exists": exists,
            "size_bytes": size_bytes,
            "row_count": row_count,
            "modified_at": mtime,
        })

    return {
        "status": "success",
        "reports": files_info,
    }


@router.get("/download/{filename}", summary="Download an official report file")
async def download_report_file(request: Request, filename: str):
    """Securely download an official report file with strict whitelist validation.
    
    Protects against directory traversal and restricts downloads strictly to the 5 official reports:
    - batch_summary.csv
    - daily_overview.csv
    - lead_report.csv
    - rejections.csv
    - data_quality.json
    """
    safe_name = Path(filename).name
    if safe_name not in ALLOWED_REPORT_FILES:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Report file '{safe_name}' not found or unauthorized.",
        )

    reports_dir: Path = getattr(request.app.state, "reports_dir", Path("reports"))
    target_path = reports_dir / safe_name

    if not target_path.exists():
        db_path: Path = getattr(request.app.state, "db_path", Path("data/ledger.db"))
        try:
            with get_db_connection(db_path) as conn:
                init_db(conn)
                generate_all_reports(conn, reports_dir)
        except Exception:
            pass

    if not target_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Report file '{safe_name}' has not been generated yet. Please upload data first.",
        )

    media_type = "application/json; charset=utf-8" if safe_name.endswith(".json") else "text/csv; charset=utf-8"
    headers = {
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    }

    return FileResponse(
        path=target_path,
        filename=safe_name,
        media_type=media_type,
        headers=headers,
    )
