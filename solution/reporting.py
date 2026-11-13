"""Report generation pipeline producing the exact five required output files."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, List

import sqlite3

from solution.ledger import get_call_summaries, get_lead_metrics


def generate_batch_summary(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Generate batch_summary report data:
    
    Columns: workflow_id,batch_id,total_leads,yet_to_reach_out,in_progress_telephony_failed,in_progress_dialed,completed
    Sorted by: workflow_id, batch_id
    """
    lead_metrics = get_lead_metrics(conn)

    # Fetch all leads with their permanent batch_id
    leads_rows = conn.execute(
        "SELECT id, workflow_id, batch_id FROM leads ORDER BY workflow_id, batch_id"
    ).fetchall()

    # Aggregate counts per (workflow_id, batch_id)
    batches: Dict[tuple[str, str], Dict[str, int]] = {}
    for row in leads_rows:
        key = (row["workflow_id"], row["batch_id"])
        if key not in batches:
            batches[key] = {
                "total_leads": 0,
                "yet_to_reach_out": 0,
                "in_progress_telephony_failed": 0,
                "in_progress_dialed": 0,
                "completed": 0,
            }
        batches[key]["total_leads"] += 1
        lm = lead_metrics.get(row["id"])
        if lm:
            batches[key][lm.bucket] += 1

    records: List[Dict[str, Any]] = []
    for (wf_id, b_id) in sorted(batches.keys()):
        counts = batches[(wf_id, b_id)]
        records.append({
            "workflow_id": wf_id,
            "batch_id": b_id,
            "total_leads": counts["total_leads"],
            "yet_to_reach_out": counts["yet_to_reach_out"],
            "in_progress_telephony_failed": counts["in_progress_telephony_failed"],
            "in_progress_dialed": counts["in_progress_dialed"],
            "completed": counts["completed"],
        })

    return records


def generate_daily_overview(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Generate daily_overview report data:
    
    One row per workflow per IST date that has at least one call.
    Columns: workflow_id,date,total_calls,connected_calls,connectivity_rate,success_leads
    Sorted by: workflow_id, date
    """
    call_summaries = get_call_summaries(conn)

    # Group calls by (workflow_id, call_date_ist)
    daily_groups: Dict[tuple[str, str], List[Any]] = {}
    for call in call_summaries.values():
        key = (call.workflow_id, call.call_date_ist)
        daily_groups.setdefault(key, []).append(call)

    records: List[Dict[str, Any]] = []
    for (wf_id, call_date) in sorted(daily_groups.keys()):
        calls = daily_groups[(wf_id, call_date)]
        total_calls = len(calls)
        if total_calls == 0:
            continue

        connected_calls = sum(1 for c in calls if c.is_connected)
        rate = connected_calls / total_calls
        connectivity_rate = f"{rate:.4f}"

        # Distinct leads with at least one call on that date whose effective disposition has is_success=true
        success_leads_set = {c.lead_id for c in calls if c.is_success}
        success_leads = len(success_leads_set)

        records.append({
            "workflow_id": wf_id,
            "date": call_date,
            "total_calls": total_calls,
            "connected_calls": connected_calls,
            "connectivity_rate": connectivity_rate,
            "success_leads": success_leads,
        })

    return records


def generate_lead_report(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Generate lead_report data:
    
    One row per lead.
    Columns: workflow_id,external_ref,batch_id,phone_masked,bucket,total_calls,connected_calls,last_call_at,current_disposition,ever_success
    Sorted by: workflow_id, external_ref
    """
    lead_metrics = get_lead_metrics(conn)

    leads_rows = conn.execute(
        """
        SELECT id, workflow_id, external_ref, batch_id, phone_masked
        FROM leads
        ORDER BY workflow_id, external_ref
        """
    ).fetchall()

    records: List[Dict[str, Any]] = []
    for row in leads_rows:
        lid = row["id"]
        lm = lead_metrics.get(lid)

        total_calls = lm.total_calls if lm else 0
        connected_calls = lm.connected_calls if lm else 0
        last_call_at = (lm.last_call_at_ist if lm and lm.last_call_at_ist else "")
        current_disp = (lm.current_disposition if lm and lm.current_disposition else "")
        ever_success = "true" if (lm and lm.ever_success) else "false"
        bucket = lm.bucket if lm else "yet_to_reach_out"

        records.append({
            "workflow_id": row["workflow_id"],
            "external_ref": row["external_ref"],
            "batch_id": row["batch_id"],
            "phone_masked": row["phone_masked"],
            "bucket": bucket,
            "total_calls": total_calls,
            "connected_calls": connected_calls,
            "last_call_at": last_call_at,
            "current_disposition": current_disp,
            "ever_success": ever_success,
        })

    return records


def generate_rejections_report(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Generate rejections report data:
    
    Columns: workflow_id,batch_id,row_number,reason
    Sorted by: workflow_id, batch_id, row_number
    Strictly NO PII.
    """
    rows = conn.execute(
        """
        SELECT workflow_id, batch_id, row_number, reason
        FROM rejections
        ORDER BY workflow_id, batch_id, row_number ASC
        """
    ).fetchall()

    return [
        {
            "workflow_id": r["workflow_id"],
            "batch_id": r["batch_id"],
            "row_number": r["row_number"],
            "reason": r["reason"],
        }
        for r in rows
    ]


def generate_data_quality_report(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Generate data_quality.json dictionary matching exact schema:
    
    {
      "lead_rows_total": 0,
      "lead_rows_rejected": 0,
      "rejections_by_reason": {"invalid_phone": 0, "missing_required:customer_name": 0},
      "duplicate_lead_rows_merged": 0,
      "field_warnings": 0,
      "events_total": 0,
      "malformed_events": 0,
      "duplicate_events_dropped": 0,
      "orphan_events": 0,
      "unknown_disposition_events": 0
    }
    """
    # Aggregate lead metrics from file_metrics
    lead_stats = conn.execute(
        """
        SELECT 
            COALESCE(SUM(lead_rows_total), 0) AS lead_rows_total,
            COALESCE(SUM(duplicate_lead_rows_merged), 0) AS duplicate_lead_rows_merged,
            COALESCE(SUM(field_warnings), 0) AS field_warnings
        FROM file_metrics WHERE file_type = 'leads'
        """
    ).fetchone()

    # Rejections total and grouped reasons from rejections table
    rej_count = conn.execute("SELECT COUNT(*) AS c FROM rejections").fetchone()["c"]
    rej_reasons_rows = conn.execute(
        "SELECT reason, COUNT(*) AS c FROM rejections GROUP BY reason ORDER BY reason"
    ).fetchall()
    rejections_by_reason = {r["reason"]: r["c"] for r in rej_reasons_rows}

    # Aggregate event metrics from file_metrics
    event_stats = conn.execute(
        """
        SELECT 
            COALESCE(SUM(events_total), 0) AS events_total,
            COALESCE(SUM(malformed_events), 0) AS malformed_events,
            COALESCE(SUM(duplicate_events_dropped), 0) AS duplicate_events_dropped,
            COALESCE(SUM(unknown_disposition_events), 0) AS unknown_disposition_events
        FROM file_metrics WHERE file_type = 'events'
        """
    ).fetchone()

    # Final orphan events directly from interaction_ledger
    # Spec: "Orphan counts in data_quality.json reflect the final state."
    orphan_count = conn.execute(
        "SELECT COUNT(*) AS c FROM interaction_ledger WHERE is_orphan = 1"
    ).fetchone()["c"]

    return {
        "lead_rows_total": lead_stats["lead_rows_total"],
        "lead_rows_rejected": rej_count,
        "rejections_by_reason": rejections_by_reason,
        "duplicate_lead_rows_merged": lead_stats["duplicate_lead_rows_merged"],
        "field_warnings": lead_stats["field_warnings"],
        "events_total": event_stats["events_total"],
        "malformed_events": event_stats["malformed_events"],
        "duplicate_events_dropped": event_stats["duplicate_events_dropped"],
        "orphan_events": orphan_count,
        "unknown_disposition_events": event_stats["unknown_disposition_events"],
    }


def write_csv_file(path: Path, fieldnames: List[str], rows: List[Dict[str, Any]]) -> None:
    """Write list of dictionaries to CSV with exact header and no index."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for r in rows:
            writer.writerow(r)


def generate_all_reports(conn: sqlite3.Connection, out_dir: Path) -> Dict[str, Path]:
    """Generate and write all five reports into out_dir."""
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. batch_summary.csv
    batch_summary_fields = [
        "workflow_id",
        "batch_id",
        "total_leads",
        "yet_to_reach_out",
        "in_progress_telephony_failed",
        "in_progress_dialed",
        "completed",
    ]
    batch_summary_data = generate_batch_summary(conn)
    batch_summary_path = out_dir / "batch_summary.csv"
    write_csv_file(batch_summary_path, batch_summary_fields, batch_summary_data)

    # 2. daily_overview.csv
    daily_overview_fields = [
        "workflow_id",
        "date",
        "total_calls",
        "connected_calls",
        "connectivity_rate",
        "success_leads",
    ]
    daily_overview_data = generate_daily_overview(conn)
    daily_overview_path = out_dir / "daily_overview.csv"
    write_csv_file(daily_overview_path, daily_overview_fields, daily_overview_data)

    # 3. lead_report.csv
    lead_report_fields = [
        "workflow_id",
        "external_ref",
        "batch_id",
        "phone_masked",
        "bucket",
        "total_calls",
        "connected_calls",
        "last_call_at",
        "current_disposition",
        "ever_success",
    ]
    lead_report_data = generate_lead_report(conn)
    lead_report_path = out_dir / "lead_report.csv"
    write_csv_file(lead_report_path, lead_report_fields, lead_report_data)

    # 4. rejections.csv
    rejections_fields = [
        "workflow_id",
        "batch_id",
        "row_number",
        "reason",
    ]
    rejections_data = generate_rejections_report(conn)
    rejections_path = out_dir / "rejections.csv"
    write_csv_file(rejections_path, rejections_fields, rejections_data)

    # 5. data_quality.json
    data_quality_data = generate_data_quality_report(conn)
    data_quality_path = out_dir / "data_quality.json"
    with open(data_quality_path, "w", encoding="utf-8") as f:
        json.dump(data_quality_data, f, indent=2)

    return {
        "batch_summary": batch_summary_path,
        "daily_overview": daily_overview_path,
        "lead_report": lead_report_path,
        "rejections": rejections_path,
        "data_quality": data_quality_path,
    }
