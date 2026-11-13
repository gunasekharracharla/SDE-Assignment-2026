"""Dashboard, Leads, and Ledger query routes."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from solution.db import get_db_connection, init_db
from solution.events import parse_event_line
from solution.ledger import get_call_summaries, get_lead_metrics, reconcile_orphan_events
from solution.reporting import generate_all_reports, generate_data_quality_report
from solution.schemas import load_dispositions
from solution.web.config import ALLOWED_WORKFLOWS

logger = logging.getLogger("web.dashboard")

router = APIRouter()


class OverrideRequest(BaseModel):
    lead_id: int
    call_id: str = Field(..., min_length=1, max_length=100)
    disposition_code: str = Field(..., min_length=1, max_length=100)
    reason: Optional[str] = Field("", max_length=500)


@router.get("/stats", summary="Get aggregated dashboard statistics and chart data")
async def get_dashboard_stats(request: Request) -> Dict[str, Any]:
    """Retrieve actual KPI metrics, bucket distribution, daily activity, and data quality directly from SQLite."""
    db_path: Path = getattr(request.app.state, "db_path", Path("data/ledger.db"))

    with get_db_connection(db_path) as conn:
        init_db(conn)

        # 1. Leads and Ingestion Reconciliation Metrics
        unique_leads_row = conn.execute("SELECT COUNT(*) as c FROM leads").fetchone()
        unique_leads = unique_leads_row["c"] if unique_leads_row else 0

        rejections_count_row = conn.execute("SELECT COUNT(*) as c FROM rejections").fetchone()
        rejected_leads = rejections_count_row["c"] if rejections_count_row else 0

        # Retrieve file_metrics summary via data quality logic
        dq_report = generate_data_quality_report(conn)
        input_rows_processed = dq_report["lead_rows_total"]
        duplicate_lead_rows_merged = dq_report["duplicate_lead_rows_merged"]

        # In case file_metrics is not populated (e.g. ad-hoc DB insertion), fallback cleanly
        if input_rows_processed == 0 and (unique_leads > 0 or rejected_leads > 0):
            input_rows_processed = unique_leads + duplicate_lead_rows_merged + rejected_leads

        # 2. Call Summaries & Lead Metrics
        call_summaries = get_call_summaries(conn)
        total_calls = len(call_summaries)
        connected_calls = sum(1 for c in call_summaries.values() if c.is_connected)

        lead_metrics = get_lead_metrics(conn)
        successful_leads = sum(1 for lm in lead_metrics.values() if lm.ever_success)

        # 3. Active Orphans
        orphan_row = conn.execute(
            "SELECT COUNT(*) as c FROM interaction_ledger WHERE is_orphan = 1"
        ).fetchone()
        active_orphans = orphan_row["c"] if orphan_row else 0

        # 4. Bucket Distribution
        bucket_counts = {
            "yet_to_reach_out": 0,
            "completed": 0,
            "in_progress_dialed": 0,
            "in_progress_telephony_failed": 0,
        }
        for lm in lead_metrics.values():
            if lm.bucket in bucket_counts:
                bucket_counts[lm.bucket] += 1
            else:
                bucket_counts[lm.bucket] = 1

        # 5. Daily Activity (from valid calls grouped by IST date)
        daily_groups: Dict[str, Dict[str, Any]] = {}
        for c in call_summaries.values():
            d = c.call_date_ist
            if d not in daily_groups:
                daily_groups[d] = {"date": d, "total_calls": 0, "connected_calls": 0, "success_leads_set": set()}
            daily_groups[d]["total_calls"] += 1
            if c.is_connected:
                daily_groups[d]["connected_calls"] += 1
            if c.is_success:
                daily_groups[d]["success_leads_set"].add(c.lead_id)

        daily_activity = []
        for d in sorted(daily_groups.keys()):
            item = daily_groups[d]
            t = item["total_calls"]
            conn_c = item["connected_calls"]
            rate = f"{(conn_c / t):.4f}" if t > 0 else "0.0000"
            daily_activity.append({
                "date": d,
                "total_calls": t,
                "connected_calls": conn_c,
                "connectivity_rate": rate,
                "success_leads": len(item["success_leads_set"]),
            })

        # 6. Rejections Breakdown by Reason
        rej_rows = conn.execute(
            "SELECT reason, COUNT(*) as c FROM rejections GROUP BY reason ORDER BY c DESC"
        ).fetchall()
        rejections_by_reason = {r["reason"]: r["c"] for r in rej_rows}

    return {
        "status": "success",
        "summary": {
            # Explicit, unambiguous metrics
            "unique_leads": unique_leads,
            "input_rows_processed": input_rows_processed,
            "duplicate_lead_rows_merged": duplicate_lead_rows_merged,
            "rejected_leads": rejected_leads,
            # Backward-compatibility keys
            "accepted_leads": unique_leads,
            "total_leads": unique_leads,
            "lead_rows_total": input_rows_processed,
            "field_warnings": dq_report["field_warnings"],
            "total_calls": total_calls,
            "connected_calls": connected_calls,
            "connectivity_rate": f"{(connected_calls / total_calls):.4f}" if total_calls > 0 else "0.0000",
            "successful_leads": successful_leads,
            "events_total": dq_report["events_total"],
            "malformed_events": dq_report["malformed_events"],
            "duplicate_events_dropped": dq_report["duplicate_events_dropped"],
            "unknown_disposition_events": dq_report["unknown_disposition_events"],
            "active_orphan_events": active_orphans,
        },
        "bucket_distribution": bucket_counts,
        "daily_activity": daily_activity,
        "rejections_by_reason": rejections_by_reason,
    }


@router.get("/leads", summary="Query searchable, paginated leads with masked phone numbers")
async def get_leads_list(
    request: Request,
    q: str = Query("", description="Search term for external_ref or masked phone"),
    workflow_id: Optional[str] = Query(None, description="Filter by workflow_id"),
    bucket: Optional[str] = Query(None, description="Filter by call bucket"),
    page: int = Query(1, ge=1, description="Page number"),
    limit: int = Query(25, ge=5, le=100, description="Items per page"),
) -> Dict[str, Any]:
    """Retrieve paginated leads list strictly with masked phone numbers. Zero plaintext PII."""
    db_path: Path = getattr(request.app.state, "db_path", Path("data/ledger.db"))

    with get_db_connection(db_path) as conn:
        init_db(conn)

        # Pre-calculate lead metrics for bucketing and call counts
        lead_metrics = get_lead_metrics(conn)

        # Build dynamic SQL filters
        where_clauses: List[str] = []
        params: List[Any] = []

        if workflow_id and workflow_id in ALLOWED_WORKFLOWS:
            where_clauses.append("workflow_id = ?")
            params.append(workflow_id)

        if q.strip():
            term = f"%{q.strip()}%"
            where_clauses.append("(external_ref LIKE ? OR phone_masked LIKE ?)")
            params.extend([term, term])

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

        # Fetch candidate leads
        query = f"""
            SELECT id, workflow_id, external_ref, batch_id, phone_masked, preferred_language, created_at, updated_at
            FROM leads
            {where_sql}
            ORDER BY workflow_id, external_ref
        """
        all_candidates = conn.execute(query, params).fetchall()

        # Apply bucket filter in memory (since bucket is derived by the ledger engine)
        filtered_records = []
        for row in all_candidates:
            lid = row["id"]
            lm = lead_metrics.get(lid)
            lead_bucket = lm.bucket if lm else "yet_to_reach_out"

            if bucket and bucket.strip() and lead_bucket != bucket.strip():
                continue

            filtered_records.append({
                "id": lid,
                "workflow_id": row["workflow_id"],
                "external_ref": row["external_ref"],
                "batch_id": row["batch_id"],
                "phone_masked": row["phone_masked"],  # Strictly masked
                "bucket": lead_bucket,
                "total_calls": lm.total_calls if lm else 0,
                "connected_calls": lm.connected_calls if lm else 0,
                "last_call_at_ist": lm.last_call_at_ist if lm and lm.last_call_at_ist else "",
                "current_disposition": lm.current_disposition if lm and lm.current_disposition else "",
                "ever_success": lm.ever_success if lm else False,
                "preferred_language": row["preferred_language"] or "en",
                "created_at": row["created_at"],
            })

        total_matching = len(filtered_records)
        start_idx = (page - 1) * limit
        end_idx = start_idx + limit
        paginated_items = filtered_records[start_idx:end_idx]

    return {
        "status": "success",
        "total": total_matching,
        "page": page,
        "limit": limit,
        "total_pages": (total_matching + limit - 1) // limit if total_matching > 0 else 1,
        "leads": paginated_items,
    }


@router.get("/leads/{lead_id}/calls", summary="Get calls and allowed dispositions for a specific lead")
async def get_lead_calls(request: Request, lead_id: int) -> Dict[str, Any]:
    """Retrieve all initiated calls linked to lead_id along with allowed workflow dispositions."""
    db_path: Path = getattr(request.app.state, "db_path", Path("data/ledger.db"))

    with get_db_connection(db_path) as conn:
        init_db(conn)

        lead = conn.execute(
            "SELECT id, workflow_id, external_ref, phone_masked, batch_id FROM leads WHERE id = ?",
            (lead_id,),
        ).fetchone()
        if not lead:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Lead #{lead_id} not found.")

        wf_id = lead["workflow_id"]
        all_dispositions = load_dispositions()
        wf_dispositions = all_dispositions.get(wf_id, {})

        allowed_disps = [
            {
                "code": d.code,
                "label": d.label,
                "is_terminal": d.is_terminal,
                "is_success": d.is_success,
            }
            for d in wf_dispositions.values()
        ]

        # Fetch calls for this lead
        call_summaries = get_call_summaries(conn)
        lead_calls = [
            {
                "call_id": cs.call_id,
                "occurred_at_ist": cs.initiated_at_ist,
                "call_date_ist": cs.call_date_ist,
                "is_connected": cs.is_connected,
                "is_telephony_failed": cs.has_telephony_failed,
                "effective_disposition": cs.effective_disposition or "—",
                "is_terminal": cs.is_terminal,
                "is_success": cs.is_success,
            }
            for cs in call_summaries.values()
            if cs.lead_id == lead_id
        ]
        lead_calls.sort(key=lambda c: c["occurred_at_ist"], reverse=True)

        return {
            "status": "success",
            "lead": {
                "id": lead["id"],
                "workflow_id": lead["workflow_id"],
                "external_ref": lead["external_ref"],
                "phone_masked": lead["phone_masked"],
            },
            "calls": lead_calls,
            "allowed_dispositions": allowed_disps,
        }


@router.post("/calls/override", summary="Record a manual disposition outcome override for a call")
async def override_call_disposition(request: Request, body: OverrideRequest) -> Dict[str, Any]:
    """Append a disposition.override event for an existing initiated call belonging to lead_id."""
    db_path: Path = getattr(request.app.state, "db_path", Path("data/ledger.db"))
    reports_dir: Path = getattr(request.app.state, "reports_dir", Path("reports"))

    call_id_clean = body.call_id.strip()
    disp_code_clean = body.disposition_code.strip()
    reason_clean = (body.reason or "").strip()

    if not call_id_clean:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="call_id cannot be blank.")
    if not disp_code_clean:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="disposition_code cannot be blank.")
    if len(reason_clean) > 500:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="reason exceeds maximum length of 500 characters.")

    all_dispositions = load_dispositions()

    with get_db_connection(db_path) as conn:
        init_db(conn)

        # 1. Verify lead exists
        lead = conn.execute(
            "SELECT id, workflow_id, phone_hash, phone_masked FROM leads WHERE id = ?",
            (body.lead_id,),
        ).fetchone()
        if not lead:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Lead #{body.lead_id} not found.")

        wf_id = lead["workflow_id"]
        wf_dispositions = all_dispositions.get(wf_id, {})
        if disp_code_clean not in wf_dispositions:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Disposition '{disp_code_clean}' is not valid for workflow '{wf_id}'. Allowed: {list(wf_dispositions.keys())}",
            )

        # 2. Verify call belongs to this lead
        call_row = conn.execute(
            """
            SELECT call_id, workflow_id, lead_id, phone_hash, phone_masked
            FROM interaction_ledger
            WHERE lead_id = ? AND call_id = ? AND event_type = 'call.initiated' AND is_orphan = 0
            LIMIT 1
            """,
            (body.lead_id, call_id_clean),
        ).fetchone()
        if not call_row:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Call '{call_id_clean}' does not belong to lead #{body.lead_id} or is not a valid initiated call.",
            )

        # 3. Check for exact duplicate override (same call_id, same disposition, already active)
        call_summaries = get_call_summaries(conn)
        cs = call_summaries.get(call_id_clean)
        if cs and cs.effective_disposition == disp_code_clean:
            latest_override = conn.execute(
                """
                SELECT disposition_code FROM interaction_ledger
                WHERE call_id = ? AND event_type = 'disposition.override'
                ORDER BY occurred_at_utc DESC LIMIT 1
                """,
                (call_id_clean,),
            ).fetchone()
            if latest_override and latest_override["disposition_code"] == disp_code_clean:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Call '{call_id_clean}' already has active override disposition '{disp_code_clean}'. Duplicate submission rejected.",
                )

        # 4. Construct standard JSON event line to reuse parse_event_line
        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()
        evt_id = f"evt_ovr_{int(now_dt.timestamp())}_{uuid.uuid4().hex[:6]}"

        event_dict = {
            "event_id": evt_id,
            "workflow_id": wf_id,
            "event_type": "disposition.override",
            "call_id": call_id_clean,
            "occurred_at": now_iso,
            "payload": {
                "disposition_code": disp_code_clean,
                "audit_reason": reason_clean,
                "source": "web_manual_override",
            },
        }

        # 5. Reuse existing event validation logic
        parse_status, parsed = parse_event_line(json.dumps(event_dict), all_dispositions)
        if parse_status != "valid" or parsed is None or parsed.is_unknown_disposition:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Event validation failed through core parser.",
            )

        # 6. Append to interaction_ledger preserving append-only immutability
        conn.execute(
            """
            INSERT INTO interaction_ledger (
                event_id, workflow_id, event_type, call_id,
                occurred_at_utc, occurred_at_ist, call_date_ist,
                phone_hash, phone_masked, payload_json, disposition_code,
                is_unknown_disposition, lead_id, is_orphan, batch_key, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                parsed.event_id,
                parsed.workflow_id,
                parsed.event_type,
                parsed.call_id,
                parsed.occurred_at_utc,
                parsed.occurred_at_ist,
                parsed.call_date_ist,
                lead["phone_hash"],
                lead["phone_masked"],
                parsed.payload_json,
                parsed.disposition_code,
                0,
                lead["id"],
                0,
                "web_manual_override",
                now_iso,
            ),
        )

        # Reconcile ledger
        reconcile_orphan_events(conn)

        # Commit event transaction first before regenerating reports
        conn.commit()

        # 7. Safe report regeneration
        reports_updated = False
        try:
            generate_all_reports(conn, reports_dir)
            reports_updated = True
        except Exception as rep_err:
            logger.warning(f"Report regeneration failed after override: {rep_err}")
            reports_updated = False

        # Get updated call summary
        disp_info = wf_dispositions[disp_code_clean]
        return {
            "status": "success",
            "message": f"Disposition successfully overridden to '{disp_code_clean}'",
            "event_id": parsed.event_id,
            "call_id": call_id_clean,
            "effective_disposition": disp_code_clean,
            "is_terminal": disp_info.is_terminal,
            "is_success": disp_info.is_success,
            "reports_updated": reports_updated,
        }


@router.get("/ledger", summary="Query append-only interaction ledger events")
async def get_ledger_events(
    request: Request,
    workflow_id: Optional[str] = Query(None, description="Filter by workflow_id"),
    event_type: Optional[str] = Query(None, description="Filter by event_type"),
    call_id: Optional[str] = Query(None, description="Search by call_id"),
    page: int = Query(1, ge=1, description="Page number"),
    limit: int = Query(25, ge=5, le=100, description="Items per page"),
) -> Dict[str, Any]:
    """Retrieve paginated events from interaction_ledger. Zero plaintext PII or raw phones."""
    db_path: Path = getattr(request.app.state, "db_path", Path("data/ledger.db"))

    with get_db_connection(db_path) as conn:
        init_db(conn)

        where_clauses: List[str] = []
        params: List[Any] = []

        if workflow_id and workflow_id in ALLOWED_WORKFLOWS:
            where_clauses.append("workflow_id = ?")
            params.append(workflow_id)

        if event_type and event_type.strip():
            where_clauses.append("event_type = ?")
            params.append(event_type.strip())

        if call_id and call_id.strip():
            where_clauses.append("call_id LIKE ?")
            params.append(f"%{call_id.strip()}%")

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

        # Count total matching
        count_sql = f"SELECT COUNT(*) as c FROM interaction_ledger {where_sql}"
        total_count = conn.execute(count_sql, params).fetchone()["c"]

        offset = (page - 1) * limit
        query_sql = f"""
            SELECT 
                event_id, workflow_id, event_type, call_id,
                occurred_at_ist, call_date_ist, phone_masked,
                disposition_code, is_orphan, created_at
            FROM interaction_ledger
            {where_sql}
            ORDER BY occurred_at_utc DESC
            LIMIT ? OFFSET ?
        """
        rows = conn.execute(query_sql, params + [limit, offset]).fetchall()

        events = [
            {
                "event_id": r["event_id"],
                "workflow_id": r["workflow_id"],
                "event_type": r["event_type"],
                "call_id": r["call_id"],
                "occurred_at_ist": r["occurred_at_ist"],
                "call_date_ist": r["call_date_ist"],
                "phone_masked": r["phone_masked"] or "—",
                "disposition_code": r["disposition_code"] or "—",
                "is_orphan": bool(r["is_orphan"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    return {
        "status": "success",
        "total": total_count,
        "page": page,
        "limit": limit,
        "total_pages": (total_count + limit - 1) // limit if total_count > 0 else 1,
        "events": events,
    }
