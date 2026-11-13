"""Upload routes for secure file ingestion."""

from __future__ import annotations

import csv
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status

from solution.db import get_db_connection, init_db
from solution.events import ingest_event_jsonl
from solution.leads import ingest_lead_csv
from solution.ledger import reconcile_orphan_events
from solution.reporting import generate_all_reports
from solution.schemas import load_schemas, normalize_header
from solution.web.config import (
    ALLOWED_EXTENSIONS_EVENTS,
    ALLOWED_EXTENSIONS_LEADS,
    ALLOWED_WORKFLOWS,
    MAX_UPLOAD_BYTES,
    sanitize_filename,
    save_stream_safely,
    validate_workflow_id,
)

router = APIRouter()


def _validate_lead_csv_headers(csv_path: Path, workflow_id: str) -> None:
    """Validate that the uploaded CSV contains valid headers matching the workflow schema."""
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        header_row = next(reader, None)
        if not header_row or not any(h.strip() for h in header_row):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Uploaded CSV file is empty or has no header row.",
            )

        schemas = load_schemas()
        schema = schemas.get(workflow_id)
        if schema:
            header_map = schema.build_header_map()
            matched_fields = {
                header_map[normalize_header(h)]
                for h in header_row
                if normalize_header(h) in header_map
            }
            required_fields = {f.name for f in schema.fields if f.required}
            if not (required_fields & matched_fields):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Missing required CSV column headers for workflow '{workflow_id}'. Expected at least some of: {sorted(list(required_fields))}",
                )


@router.post("/leads", status_code=status.HTTP_200_OK, summary="Upload and ingest lead CSV file")
async def upload_leads_csv(
    request: Request,
    workflow_id: str = Form(..., description="Target workflow: 'motor_renewal' or 'policy_feedback'"),
    file: UploadFile = File(..., description="CSV file containing customer leads"),
) -> Dict[str, Any]:
    """Securely upload and ingest a CSV lead file for the selected workflow.
    
    Security & Validation:
    - Strictly validates workflow_id against allowed schemas.
    - Validates file extension is .csv.
    - Enforces 50 MB upload limit while streaming (preventing memory exhaustion).
    - Sanitizes filename to prevent directory traversal attacks.
    - Stages uploads outside public web root.
    - Zero plaintext PII or encryption keys returned.
    """
    # 1. Validate workflow selection
    wf_norm = workflow_id.strip()
    if not validate_workflow_id(wf_norm):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid workflow_id '{workflow_id}'. Must be one of: {list(ALLOWED_WORKFLOWS)}",
        )

    # 2. Validate file extension
    raw_filename = file.filename or ""
    suffix = Path(raw_filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS_LEADS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid file extension '{suffix}'. Only .csv files are supported for lead ingestion.",
        )

    # 3. Create isolated staging folder
    uploads_dir: Path = getattr(request.app.state, "uploads_dir", Path("data/uploads"))
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    stage_dir = uploads_dir / f"stage_lead_{timestamp}"
    stage_dir.mkdir(parents=True, exist_ok=True)

    safe_name = sanitize_filename(raw_filename)
    dest_path = stage_dir / safe_name

    # 4. Stream upload with strict size limit enforcement
    try:
        file_size = await save_stream_safely(file, dest_path, max_bytes=MAX_UPLOAD_BYTES)
        if file_size == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Uploaded file is empty (0 bytes).",
            )
        _validate_lead_csv_headers(dest_path, wf_norm)
    except HTTPException:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise

    # 5. Process through existing lead ingestion pipeline
    db_path: Path = getattr(request.app.state, "db_path", Path("data/ledger.db"))
    reports_dir: Path = getattr(request.app.state, "reports_dir", Path("reports"))
    batch_prefix = "web_upload"
    file_key = f"{batch_prefix}/{wf_norm}/{dest_path.name}"

    try:
        with get_db_connection(db_path) as conn:
            init_db(conn)

            # Ingest lead CSV using core business logic
            metrics = ingest_lead_csv(
                csv_path=dest_path,
                workflow_id=wf_norm,
                conn=conn,
                batch_key_prefix=batch_prefix,
            )

            # Fetch specific rejections for this upload (Strictly NO PII)
            rej_cursor = conn.execute(
                """
                SELECT row_number, reason 
                FROM rejections 
                WHERE batch_key = ? 
                ORDER BY row_number ASC
                """,
                (file_key,),
            )
            rej_rows = rej_cursor.fetchall()

            # Compile rejection summary
            rejections_by_reason: Dict[str, int] = {}
            rejection_details: List[Dict[str, Any]] = []
            for r in rej_rows:
                reason = r["reason"]
                rejections_by_reason[reason] = rejections_by_reason.get(reason, 0) + 1
                if len(rejection_details) < 100:  # Cap preview to first 100 rejections
                    rejection_details.append({
                        "row_number": r["row_number"],
                        "reason": reason,
                    })

            # Regenerate reports with updated database state
            generate_all_reports(conn, reports_dir)

    except ValueError as ve:
        # Schema or configuration error
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))
    except Exception as exc:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Ingestion processing failed: {type(exc).__name__}",
        )

    accepted_rows = metrics["lead_rows_total"] - metrics["lead_rows_rejected"]

    return {
        "status": "success",
        "workflow_id": wf_norm,
        "filename": safe_name,
        "file_size_bytes": file_size,
        "metrics": {
            "lead_rows_total": metrics["lead_rows_total"],
            "lead_rows_accepted": accepted_rows,
            "lead_rows_rejected": metrics["lead_rows_rejected"],
            "duplicate_lead_rows_merged": metrics["duplicate_lead_rows_merged"],
            "field_warnings": metrics["field_warnings"],
        },
        "rejections_by_reason": rejections_by_reason,
        "rejection_details": rejection_details,
        "reports_updated": True,
    }


@router.post("/events", status_code=status.HTTP_200_OK, summary="Upload and ingest event JSONL file")
async def upload_events_jsonl(
    request: Request,
    file: UploadFile = File(..., description="JSONL file containing interaction events"),
) -> Dict[str, Any]:
    """Securely upload and ingest interaction event JSON Lines stream.
    
    Security & Validation:
    - Validates file extension is .jsonl or .json.
    - Enforces 50 MB upload limit while streaming.
    - Sanitizes filename to prevent directory traversal.
    - Stages uploads outside public web root.
    - Idempotent deduplication on event_id.
    - Auto-reconciles orphan events and regenerates reports.
    - Zero plaintext PII returned.
    """
    raw_filename = file.filename or ""
    suffix = Path(raw_filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS_EVENTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid file extension '{suffix}'. Only .jsonl or .json files are supported for event ingestion.",
        )

    uploads_dir: Path = getattr(request.app.state, "uploads_dir", Path("data/uploads"))
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    stage_dir = uploads_dir / f"stage_event_{timestamp}"
    stage_dir.mkdir(parents=True, exist_ok=True)

    safe_name = sanitize_filename(raw_filename)
    dest_path = stage_dir / safe_name

    try:
        file_size = await save_stream_safely(file, dest_path, max_bytes=MAX_UPLOAD_BYTES)
        if file_size == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Uploaded file is empty (0 bytes).",
            )
    except HTTPException:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise

    db_path: Path = getattr(request.app.state, "db_path", Path("data/ledger.db"))
    reports_dir: Path = getattr(request.app.state, "reports_dir", Path("reports"))
    batch_prefix = "web_upload"

    try:
        with get_db_connection(db_path) as conn:
            init_db(conn)

            # Ingest events using existing business logic
            metrics = ingest_event_jsonl(
                jsonl_path=dest_path,
                conn=conn,
                batch_key_prefix=batch_prefix,
            )

            # Reconcile any orphan events
            reconcile_orphan_events(conn)

            # Query active orphan count directly from interaction_ledger
            orphan_row = conn.execute(
                "SELECT COUNT(*) as c FROM interaction_ledger WHERE is_orphan = 1"
            ).fetchone()
            active_orphans = orphan_row["c"] if orphan_row else 0

            # Regenerate reports with updated event data
            generate_all_reports(conn, reports_dir)

    except Exception as exc:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Event ingestion processing failed: {type(exc).__name__}",
        )

    events_total = metrics["events_total"]
    malformed = metrics["malformed_events"]
    dropped_dups = metrics["duplicate_events_dropped"]
    processed = events_total - malformed - dropped_dups

    return {
        "status": "success",
        "filename": safe_name,
        "file_size_bytes": file_size,
        "metrics": {
            "events_total": events_total,
            "events_processed": processed,
            "malformed_events": malformed,
            "duplicate_events_dropped": dropped_dups,
            "unknown_disposition_events": metrics["unknown_disposition_events"],
            "active_orphan_events": active_orphans,
        },
        "reports_updated": True,
    }


@router.post("/bundle", status_code=status.HTTP_200_OK, summary="Upload and ingest leads CSV and optional events JSONL together")
async def upload_bundle(
    request: Request,
    workflow_id: str = Form(..., description="Target workflow: 'motor_renewal' or 'policy_feedback'"),
    leads_file: UploadFile = File(..., description="CSV file containing customer leads"),
    events_file: UploadFile | None = File(None, description="Optional JSONL file containing interaction events"),
) -> Dict[str, Any]:
    """Securely upload and process both customer leads and interaction events in a single coordinated pipeline.
    
    Processing Order:
    1. Validate workflow and file extensions.
    2. Stream files safely to isolated staging directories enforcing 50 MB limits.
    3. Ingest customer leads (schema-order validation, AES-GCM PII encryption, duplicate merging).
    4. Ingest interaction events (append-only ledger, duplicate event_id drop, unknown disposition isolation).
    5. Execute bidirectional orphan event reconciliation.
    6. Regenerate all five official reports (batch_summary, daily_overview, lead_report, rejections, data_quality).
    7. Return zero-PII processing telemetry.
    """
    wf_norm = workflow_id.strip()
    if not validate_workflow_id(wf_norm):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid workflow_id '{workflow_id}'. Must be one of: {list(ALLOWED_WORKFLOWS)}",
        )

    # Validate leads file
    leads_raw_name = leads_file.filename or ""
    leads_suffix = Path(leads_raw_name).suffix.lower()
    if leads_suffix not in ALLOWED_EXTENSIONS_LEADS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid leads file extension '{leads_suffix}'. Only .csv files are supported.",
        )

    # Validate optional events file
    has_events = events_file is not None and bool(events_file.filename)
    events_raw_name = events_file.filename if has_events else ""
    if has_events:
        events_suffix = Path(events_raw_name).suffix.lower()
        if events_suffix not in ALLOWED_EXTENSIONS_EVENTS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid events file extension '{events_suffix}'. Only .jsonl or .json files are supported.",
            )

    uploads_dir: Path = getattr(request.app.state, "uploads_dir", Path("data/uploads"))
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    stage_dir = uploads_dir / f"stage_bundle_{timestamp}"
    stage_dir.mkdir(parents=True, exist_ok=True)

    safe_leads_name = sanitize_filename(leads_raw_name)
    leads_dest_path = stage_dir / safe_leads_name

    try:
        leads_size = await save_stream_safely(leads_file, leads_dest_path, max_bytes=MAX_UPLOAD_BYTES)
        if leads_size == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Uploaded leads file is empty (0 bytes).",
            )
        _validate_lead_csv_headers(leads_dest_path, wf_norm)
        events_dest_path = None
        events_size = 0
        if has_events and events_file is not None:
            safe_events_name = sanitize_filename(events_raw_name)
            events_dest_path = stage_dir / safe_events_name
            events_size = await save_stream_safely(events_file, events_dest_path, max_bytes=MAX_UPLOAD_BYTES)
            if events_size == 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Uploaded events file is empty (0 bytes).",
                )
    except HTTPException:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise

    db_path: Path = getattr(request.app.state, "db_path", Path("data/ledger.db"))
    reports_dir: Path = getattr(request.app.state, "reports_dir", Path("reports"))
    batch_prefix = "web_upload"
    file_key = f"{batch_prefix}/{wf_norm}/{leads_dest_path.name}"

    try:
        with get_db_connection(db_path) as conn:
            init_db(conn)

            # 1. Ingest Leads
            lead_metrics = ingest_lead_csv(
                csv_path=leads_dest_path,
                workflow_id=wf_norm,
                conn=conn,
                batch_key_prefix=batch_prefix,
            )

            # 2. Ingest Events (if provided)
            event_metrics = None
            if events_dest_path and events_dest_path.exists():
                event_metrics = ingest_event_jsonl(
                    jsonl_path=events_dest_path,
                    conn=conn,
                    batch_key_prefix=batch_prefix,
                )

            # 3. Final Orphan Event Reconciliation
            reconcile_orphan_events(conn)

            # 4. Query Rejections (Strictly NO PII)
            rej_cursor = conn.execute(
                """
                SELECT row_number, reason 
                FROM rejections 
                WHERE batch_key = ? 
                ORDER BY row_number ASC
                """,
                (file_key,),
            )
            rej_rows = rej_cursor.fetchall()
            rejections_by_reason: Dict[str, int] = {}
            rejection_details: List[Dict[str, Any]] = []
            for r in rej_rows:
                reason = r["reason"]
                rejections_by_reason[reason] = rejections_by_reason.get(reason, 0) + 1
                if len(rejection_details) < 100:
                    rejection_details.append({
                        "row_number": r["row_number"],
                        "reason": reason,
                    })

            # 5. Query Active Orphans
            orphan_row = conn.execute(
                "SELECT COUNT(*) as c FROM interaction_ledger WHERE is_orphan = 1"
            ).fetchone()
            active_orphans = orphan_row["c"] if orphan_row else 0

            # 6. Regenerate All Reports
            generate_all_reports(conn, reports_dir)

    except ValueError as ve:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))
    except Exception as exc:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Bundle ingestion processing failed: {type(exc).__name__}",
        )

    accepted_leads = lead_metrics["lead_rows_total"] - lead_metrics["lead_rows_rejected"]
    response_payload: Dict[str, Any] = {
        "status": "success",
        "workflow_id": wf_norm,
        "leads": {
            "filename": safe_leads_name,
            "file_size_bytes": leads_size,
            "metrics": {
                "lead_rows_total": lead_metrics["lead_rows_total"],
                "lead_rows_accepted": accepted_leads,
                "lead_rows_rejected": lead_metrics["lead_rows_rejected"],
                "duplicate_lead_rows_merged": lead_metrics["duplicate_lead_rows_merged"],
                "field_warnings": lead_metrics["field_warnings"],
            },
            "rejections_by_reason": rejections_by_reason,
            "rejection_details": rejection_details,
        },
        "reports_updated": True,
    }

    if event_metrics:
        ev_total = event_metrics["events_total"]
        ev_malformed = event_metrics["malformed_events"]
        ev_dropped = event_metrics["duplicate_events_dropped"]
        ev_processed = ev_total - ev_malformed - ev_dropped
        response_payload["events"] = {
            "filename": sanitize_filename(events_raw_name),
            "file_size_bytes": events_size,
            "metrics": {
                "events_total": ev_total,
                "events_processed": ev_processed,
                "malformed_events": ev_malformed,
                "duplicate_events_dropped": ev_dropped,
                "unknown_disposition_events": event_metrics["unknown_disposition_events"],
                "active_orphan_events": active_orphans,
            },
        }

    return response_payload
