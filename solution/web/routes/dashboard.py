"""Dashboard, Leads, and Ledger query routes."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query, Request

from solution.db import get_db_connection, init_db
from solution.ledger import get_call_summaries, get_lead_metrics
from solution.reporting import generate_data_quality_report
from solution.web.config import ALLOWED_WORKFLOWS

router = APIRouter()


@router.get("/stats", summary="Get aggregated dashboard statistics and chart data")
async def get_dashboard_stats(request: Request) -> Dict[str, Any]:
    """Retrieve actual KPI metrics, bucket distribution, daily activity, and data quality directly from SQLite."""
    db_path: Path = getattr(request.app.state, "db_path", Path("data/ledger.db"))

    with get_db_connection(db_path) as conn:
        init_db(conn)

        # 1. Total and Rejected Leads
        total_leads_row = conn.execute("SELECT COUNT(*) as c FROM leads").fetchone()
        accepted_leads = total_leads_row["c"] if total_leads_row else 0

        rejections_count_row = conn.execute("SELECT COUNT(*) as c FROM rejections").fetchone()
        rejected_leads = rejections_count_row["c"] if rejections_count_row else 0

        # Total rows presented = accepted + rejected
        total_lead_records = accepted_leads + rejected_leads

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

        # 7. File Metrics Summary (Duplicates merged, malformed events, etc.)
        dq_report = generate_data_quality_report(conn)

    return {
        "status": "success",
        "summary": {
            "total_leads": total_lead_records,
            "accepted_leads": accepted_leads,
            "rejected_leads": rejected_leads,
            "duplicate_lead_rows_merged": dq_report["duplicate_lead_rows_merged"],
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
