"""Unit tests for report generation pipeline and official output contract."""

import csv
import json
import sqlite3
import pytest
from pathlib import Path

from solution.db import get_db_connection, init_db
from solution.events import ingest_event_jsonl
from solution.leads import ingest_lead_csv
from solution.reporting import (
    generate_all_reports,
    generate_batch_summary,
    generate_daily_overview,
    generate_data_quality_report,
    generate_lead_report,
    generate_rejections_report,
)


@pytest.fixture
def memory_db():
    """Create an in-memory database initialized with schema."""
    with get_db_connection(":memory:") as conn:
        init_db(conn)
        yield conn


def test_empty_database_reports(memory_db, tmp_path):
    """Verify report generation on an empty database produces exact files with headers and zero rows."""
    out_dir = tmp_path / "reports_empty"
    report_paths = generate_all_reports(memory_db, out_dir)

    expected_files = [
        "batch_summary.csv",
        "daily_overview.csv",
        "lead_report.csv",
        "rejections.csv",
        "data_quality.json",
    ]
    for ef in expected_files:
        assert (out_dir / ef).exists(), f"File {ef} missing"

    # Check batch_summary.csv header
    with open(out_dir / "batch_summary.csv", "r", encoding="utf-8") as f:
        reader = list(csv.reader(f))
        assert reader[0] == ["workflow_id", "batch_id", "total_leads", "yet_to_reach_out", "in_progress_telephony_failed", "in_progress_dialed", "completed"]
        assert len(reader) == 1

    # Check daily_overview.csv header
    with open(out_dir / "daily_overview.csv", "r", encoding="utf-8") as f:
        reader = list(csv.reader(f))
        assert reader[0] == ["workflow_id", "date", "total_calls", "connected_calls", "connectivity_rate", "success_leads"]
        assert len(reader) == 1

    # Check lead_report.csv header
    with open(out_dir / "lead_report.csv", "r", encoding="utf-8") as f:
        reader = list(csv.reader(f))
        assert reader[0] == ["workflow_id", "external_ref", "batch_id", "phone_masked", "bucket", "total_calls", "connected_calls", "last_call_at", "current_disposition", "ever_success"]
        assert len(reader) == 1

    # Check rejections.csv header
    with open(out_dir / "rejections.csv", "r", encoding="utf-8") as f:
        reader = list(csv.reader(f))
        assert reader[0] == ["workflow_id", "batch_id", "row_number", "reason"]
        assert len(reader) == 1

    # Check data_quality.json structure
    with open(out_dir / "data_quality.json", "r", encoding="utf-8") as f:
        data = json.load(f)
        assert data["lead_rows_total"] == 0
        assert data["lead_rows_rejected"] == 0
        assert data["rejections_by_reason"] == {}
        assert data["duplicate_lead_rows_merged"] == 0
        assert data["field_warnings"] == 0
        assert data["events_total"] == 0
        assert data["malformed_events"] == 0
        assert data["duplicate_events_dropped"] == 0
        assert data["orphan_events"] == 0
        assert data["unknown_disposition_events"] == 0


def test_full_pipeline_reporting_contract(tmp_path, memory_db):
    """Verify exact output files, sorting, metrics, connectivity rate formatting, and PII masking."""
    # 1. Ingest Leads
    csv_file = tmp_path / "batch_001.csv"
    with open(csv_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["customer_name", "phone", "policy_number", "policy_expiry", "premium_amount", "preferred_language"])
        # Lead 1: Will have a successful call (MOT/2026/001)
        writer.writerow(["Aarav Sharma", "+91 98765 43210", "MOT/2026/001", "2026-10-31", "15000", "en"])
        # Lead 2: Will have telephony failed calls (MOT/2026/002)
        writer.writerow(["Diya Nair", "9123456789", "MOT/2026/002", "2026-10-31", "12000", "hi"])
        # Lead 3: Will have no calls (yet_to_reach_out) (MOT/2026/003)
        writer.writerow(["Kavya Patel", "9234567890", "MOT/2026/003", "", "", ""])
        # Row 4: Rejected row (missing name)
        writer.writerow(["", "9345678901", "MOT/2026/004", "", "", ""])

    ingest_lead_csv(csv_file, "motor_renewal", memory_db, batch_key_prefix="drop_1")

    # 2. Ingest Events
    events = [
        # Call 1 for Lead 1 on 2026-10-01: Initiated, Connected, Renewed (Success & Terminal)
        {"event_id": "e1", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "c1", "occurred_at": "2026-10-01T04:30:00Z", "phone": "+91 98765 43210"},
        {"event_id": "e2", "event_type": "call.connected", "workflow_id": "motor_renewal", "call_id": "c1", "occurred_at": "2026-10-01T04:31:00Z", "phone": "+91 98765 43210"},
        {"event_id": "e3", "event_type": "disposition.set", "workflow_id": "motor_renewal", "call_id": "c1", "occurred_at": "2026-10-01T04:35:00Z", "phone": "+91 98765 43210", "payload": {"disposition_code": "renewed"}},
        
        # Call 2 for Lead 2 on 2026-10-01: Initiated, Telephony Failed
        {"event_id": "e4", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "c2", "occurred_at": "2026-10-01T05:00:00Z", "phone": "9123456789"},
        {"event_id": "e5", "event_type": "call.telephony_failed", "workflow_id": "motor_renewal", "call_id": "c2", "occurred_at": "2026-10-01T05:01:00Z", "phone": "9123456789", "payload": {"reason": "sip_503"}},

        # Malformed event line
        "INVALID JSON",
        # Duplicate event (e1)
        {"event_id": "e1", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "c1", "occurred_at": "2026-10-01T04:30:00Z", "phone": "+91 98765 43210"},
        # Pure orphan event (call to number never uploaded)
        {"event_id": "e_orph", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "c_orph", "occurred_at": "2026-10-01T06:00:00Z", "phone": "9999999999"},
    ]

    jsonl_file = tmp_path / "events_0001.jsonl"
    with open(jsonl_file, "w", encoding="utf-8") as f:
        for ev in events:
            if isinstance(ev, dict):
                f.write(json.dumps(ev) + "\n")
            else:
                f.write(str(ev) + "\n")

    ingest_event_jsonl(jsonl_file, memory_db, batch_key_prefix="drop_1")

    # Generate all reports
    out_dir = tmp_path / "reports_test"
    generate_all_reports(memory_db, out_dir)

    # 1. Verify batch_summary.csv
    with open(out_dir / "batch_summary.csv", "r", encoding="utf-8") as f:
        reader = list(csv.DictReader(f))
        assert len(reader) == 1
        row = reader[0]
        assert row["workflow_id"] == "motor_renewal"
        assert row["batch_id"] == "batch_001"
        assert int(row["total_leads"]) == 3
        assert int(row["completed"]) == 1  # Lead 1
        assert int(row["in_progress_telephony_failed"]) == 1  # Lead 2
        assert int(row["yet_to_reach_out"]) == 1  # Lead 3
        assert int(row["in_progress_dialed"]) == 0

    # 2. Verify daily_overview.csv
    with open(out_dir / "daily_overview.csv", "r", encoding="utf-8") as f:
        reader = list(csv.DictReader(f))
        assert len(reader) == 1
        d_row = reader[0]
        assert d_row["workflow_id"] == "motor_renewal"
        assert d_row["date"] == "2026-10-01"
        assert int(d_row["total_calls"]) == 2  # Call c1 and c2
        assert int(d_row["connected_calls"]) == 1  # Only Call c1 connected
        # Exactly 4 decimal places: 1/2 = 0.5000
        assert d_row["connectivity_rate"] == "0.5000"
        # Success leads = 1 (Lead 1 had renewed)
        assert int(d_row["success_leads"]) == 1

    # 3. Verify lead_report.csv
    with open(out_dir / "lead_report.csv", "r", encoding="utf-8") as f:
        reader = list(csv.DictReader(f))
        assert len(reader) == 3
        # Sorted by external_ref: MOT/2026/001, MOT/2026/002, MOT/2026/003
        refs = [r["external_ref"] for r in reader]
        assert refs == ["MOT/2026/001", "MOT/2026/002", "MOT/2026/003"]

        l1 = reader[0]
        assert l1["phone_masked"] == "+91******3210"
        assert l1["bucket"] == "completed"
        assert int(l1["total_calls"]) == 1
        assert int(l1["connected_calls"]) == 1
        assert l1["last_call_at"] == "2026-10-01T10:00:00+05:30"
        assert l1["current_disposition"] == "renewed"
        assert l1["ever_success"] == "true"

        l2 = reader[1]
        assert l2["phone_masked"] == "+91******6789"
        assert l2["bucket"] == "in_progress_telephony_failed"
        assert int(l2["total_calls"]) == 1
        assert int(l2["connected_calls"]) == 0
        assert l2["current_disposition"] == ""
        assert l2["ever_success"] == "false"

        l3 = reader[2]
        assert l3["phone_masked"] == "+91******7890"
        assert l3["bucket"] == "yet_to_reach_out"
        assert int(l3["total_calls"]) == 0
        assert l3["last_call_at"] == ""
        assert l3["current_disposition"] == ""
        assert l3["ever_success"] == "false"

    # 4. Verify rejections.csv
    with open(out_dir / "rejections.csv", "r", encoding="utf-8") as f:
        reader = list(csv.DictReader(f))
        assert len(reader) == 1
        rej = reader[0]
        assert rej["workflow_id"] == "motor_renewal"
        assert rej["batch_id"] == "batch_001"
        assert rej["row_number"] == "4"
        assert rej["reason"] == "missing_required:customer_name"

    # 5. Verify data_quality.json
    with open(out_dir / "data_quality.json", "r", encoding="utf-8") as f:
        dq = json.load(f)
        assert dq["lead_rows_total"] == 4
        assert dq["lead_rows_rejected"] == 1
        assert dq["rejections_by_reason"] == {"missing_required:customer_name": 1}
        assert dq["duplicate_lead_rows_merged"] == 0
        assert dq["field_warnings"] == 0
        assert dq["events_total"] == 8
        assert dq["malformed_events"] == 1
        assert dq["duplicate_events_dropped"] == 1
        assert dq["orphan_events"] == 1  # The pure orphan event e_orph
        assert dq["unknown_disposition_events"] == 0
