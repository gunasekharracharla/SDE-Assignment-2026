"""Tests for Upload Notification Accuracy (Task 5).

Verifies all 8 audit scenarios:
1. Fully successful upload — all rows accepted.
2. Partially successful upload — some accepted, some rejected.
3. Completely rejected upload — zero accepted, all rejected.
4. Empty CSV upload (0-byte file and header-only file with zero data rows).
5. Malformed CSV and JSONL uploads.
6. Duplicate-only upload — valid rows processed but all merged into existing leads.
7. Database committed + report generation failure resilience.
8. Combined bundle upload with mixed outcomes and per-file telemetry.
"""

from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from solution.db import init_db
from solution.web.app import create_app


@pytest.fixture
def upload_test_env(tmp_path: Path):
    """Set up an isolated test environment with fresh SQLite DB and reports directory."""
    db_path = tmp_path / "test_ledger.db"
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    uploads_dir = tmp_path / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(str(db_path)) as conn:
        init_db(conn)

    app = create_app(
        db_path=db_path,
        reports_dir=reports_dir,
        uploads_dir=uploads_dir,
    )
    client = TestClient(app)
    return client, db_path, reports_dir, uploads_dir


# ==============================================================================
# Scenario 1: Fully successful upload — all rows accepted
# ==============================================================================
def test_scenario_1_fully_successful_upload(upload_test_env):
    """Verify that an upload where all rows are valid returns outcome='success'."""
    client, db_path, reports_dir, _ = upload_test_env

    csv_data = (
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        "Alice Sharma,9876541111,POL-S1-01,2026-12-31,10000,Honda City,en\n"
        "Bob Verma,9876542222,POL-S1-02,2026-12-31,12000,Hyundai Creta,hi\n"
    ).encode("utf-8")

    res = client.post(
        "/api/upload/leads",
        data={"workflow_id": "motor_renewal"},
        files={"file": ("valid_leads.csv", io.BytesIO(csv_data), "text/csv")},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["outcome"] == "success"
    assert data["status"] == "success"
    assert data["reports_updated"] is True
    assert data["reports_error"] is None

    metrics = data["metrics"]
    assert metrics["lead_rows_total"] == 2
    assert metrics["lead_rows_accepted"] == 2
    assert metrics["lead_rows_rejected"] == 0
    assert metrics["unique_leads_created"] == 2
    assert metrics["duplicate_lead_rows_merged"] == 0
    assert "Upload successful: 2 leads processed" in data["message"]


# ==============================================================================
# Scenario 2: Partially successful upload — some accepted, some rejected
# ==============================================================================
def test_scenario_2_partially_successful_upload(upload_test_env):
    """Verify that an upload with both valid and invalid rows returns outcome='partial_success'."""
    client, db_path, reports_dir, _ = upload_test_env

    csv_data = (
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        "Valid User 1,9876543331,POL-P1-01,2026-12-31,10000,Maruti Swift,en\n"
        ",9876543332,POL-P1-02,2026-12-31,11000,Tata Nexon,en\n"  # Missing name
        "Valid User 2,9876543333,POL-P1-03,2026-12-31,12000,Kia Seltos,en\n"
        "Bad Phone User,12345,POL-P1-04,2026-12-31,13000,Mahindra XUV,en\n"  # Invalid phone
    ).encode("utf-8")

    res = client.post(
        "/api/upload/leads",
        data={"workflow_id": "motor_renewal"},
        files={"file": ("partial_leads.csv", io.BytesIO(csv_data), "text/csv")},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["outcome"] == "partial_success"
    assert data["reports_updated"] is True

    metrics = data["metrics"]
    assert metrics["lead_rows_total"] == 4
    assert metrics["lead_rows_accepted"] == 2
    assert metrics["lead_rows_rejected"] == 2
    assert metrics["unique_leads_created"] == 2
    assert len(data["rejection_details"]) == 2
    assert "Partial upload: 2 accepted" in data["message"]
    assert "2 rejected" in data["message"]


# ==============================================================================
# Scenario 3: Completely rejected upload — zero accepted, all rejected
# ==============================================================================
def test_scenario_3_completely_rejected_upload(upload_test_env):
    """Verify that an upload where 100% of rows fail validation returns outcome='rejected'."""
    client, db_path, reports_dir, _ = upload_test_env

    csv_data = (
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        ",9876544441,POL-R-01,2026-12-31,10000,Honda,en\n"  # Missing name
        "Bad Phone 1,999,POL-R-02,2026-12-31,12000,Hyundai,en\n"  # Invalid phone
        "Missing Ref,,POL-R-03,2026-12-31,14000,Tata,en\n"  # Missing phone
    ).encode("utf-8")

    res = client.post(
        "/api/upload/leads",
        data={"workflow_id": "motor_renewal"},
        files={"file": ("all_rejected.csv", io.BytesIO(csv_data), "text/csv")},
    )
    assert res.status_code == 200
    data = res.json()
    # CRITICAL: Outcome MUST be 'rejected', NOT 'success'
    assert data["outcome"] == "rejected"
    assert data["metrics"]["lead_rows_total"] == 3
    assert data["metrics"]["lead_rows_accepted"] == 0
    assert data["metrics"]["lead_rows_rejected"] == 3
    assert data["metrics"]["unique_leads_created"] == 0
    assert "Upload rejected: All 3 lead rows failed schema validation" in data["message"]


# ==============================================================================
# Scenario 4: Empty CSV upload (0-byte and header-only)
# ==============================================================================
def test_scenario_4a_empty_0_byte_csv_upload(upload_test_env):
    """Uploading an empty 0-byte file must be rejected with HTTP 400."""
    client, _, _, _ = upload_test_env

    res = client.post(
        "/api/upload/leads",
        data={"workflow_id": "motor_renewal"},
        files={"file": ("zero_byte.csv", io.BytesIO(b""), "text/csv")},
    )
    assert res.status_code == 400
    assert "empty" in res.json()["detail"].lower()


def test_scenario_4b_header_only_csv_upload(upload_test_env):
    """Uploading a CSV file with valid headers but zero data rows must be rejected with HTTP 400."""
    client, _, _, _ = upload_test_env

    header_only_csv = "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n".encode("utf-8")

    res = client.post(
        "/api/upload/leads",
        data={"workflow_id": "motor_renewal"},
        files={"file": ("header_only.csv", io.BytesIO(header_only_csv), "text/csv")},
    )
    assert res.status_code == 400
    assert "zero data rows" in res.json()["detail"].lower()


# ==============================================================================
# Scenario 5: Malformed CSV and JSONL uploads
# ==============================================================================
def test_scenario_5a_missing_required_csv_headers(upload_test_env):
    """Uploading CSV missing required schema columns must be rejected with HTTP 400."""
    client, _, _, _ = upload_test_env

    bad_csv = "RandomHeader1,RandomHeader2\nVal1,Val2\n".encode("utf-8")
    res = client.post(
        "/api/upload/leads",
        data={"workflow_id": "motor_renewal"},
        files={"file": ("bad_headers.csv", io.BytesIO(bad_csv), "text/csv")},
    )
    assert res.status_code == 400
    assert "missing required csv column headers" in res.json()["detail"].lower()


def test_scenario_5b_all_malformed_event_lines(upload_test_env):
    """Uploading JSONL where all lines fail JSON parsing returns outcome='rejected'."""
    client, _, _, _ = upload_test_env

    corrupt_jsonl = "not a json line 1\n{broken json 2\n[invalid syntax 3\n".encode("utf-8")
    res = client.post(
        "/api/upload/events",
        files={"file": ("malformed.jsonl", io.BytesIO(corrupt_jsonl), "application/jsonl")},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["outcome"] == "rejected"
    assert data["metrics"]["events_total"] == 3
    assert data["metrics"]["events_processed"] == 0
    assert data["metrics"]["malformed_events"] == 3
    assert "Upload rejected: All 3 event lines were malformed" in data["message"]


# ==============================================================================
# Scenario 6: Duplicate-only upload — all valid rows merged into existing leads
# ==============================================================================
def test_scenario_6_duplicate_only_upload(upload_test_env):
    """Verify that re-uploading existing leads returns outcome='duplicate_only' with 0 new leads."""
    client, db_path, _, _ = upload_test_env

    # 1. First upload: create two new leads
    csv1 = (
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        "Lead Alpha,9876545551,POL-D-01,2026-12-31,10000,Model 1,en\n"
        "Lead Beta,9876545552,POL-D-02,2026-12-31,12000,Model 2,en\n"
    ).encode("utf-8")

    r1 = client.post("/api/upload/leads", data={"workflow_id": "motor_renewal"}, files={"file": ("batch_a.csv", io.BytesIO(csv1), "text/csv")})
    assert r1.status_code == 200
    assert r1.json()["outcome"] == "success"
    assert r1.json()["metrics"]["unique_leads_created"] == 2

    # 2. Second upload: upload same phone numbers with updated policy numbers in a new batch
    csv2 = (
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        "Lead Alpha Updated,9876545551,POL-D-01-V2,2027-01-31,11000,Model 1,en\n"
        "Lead Beta Updated,9876545552,POL-D-02-V2,2027-01-31,13000,Model 2,en\n"
    ).encode("utf-8")

    r2 = client.post("/api/upload/leads", data={"workflow_id": "motor_renewal"}, files={"file": ("batch_b.csv", io.BytesIO(csv2), "text/csv")})
    assert r2.status_code == 200
    data2 = r2.json()

    assert data2["outcome"] == "duplicate_only"
    metrics2 = data2["metrics"]
    assert metrics2["lead_rows_total"] == 2
    assert metrics2["lead_rows_accepted"] == 2
    assert metrics2["duplicate_lead_rows_merged"] == 2
    assert metrics2["unique_leads_created"] == 0
    assert metrics2["lead_rows_rejected"] == 0
    assert "Duplicate upload: 2 rows processed" in data2["message"]
    assert "0 new unique leads created" in data2["message"]


# ==============================================================================
# Scenario 7: Database committed + report generation failure resilience
# ==============================================================================
def test_scenario_7_report_generation_failure_preserves_committed_data(upload_test_env):
    """Verify that when generate_all_reports raises an exception, the ingested data remains committed in SQLite."""
    client, db_path, reports_dir, _ = upload_test_env

    csv_data = (
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        "Resilient User,9876546661,POL-RES-01,2026-12-31,10000,Car,en\n"
    ).encode("utf-8")

    # Simulate report generation failure
    with patch("solution.web.routes.upload.generate_all_reports", side_effect=IOError("Simulated Disk Write Failure")):
        res = client.post(
            "/api/upload/leads",
            data={"workflow_id": "motor_renewal"},
            files={"file": ("resilient_lead.csv", io.BytesIO(csv_data), "text/csv")},
        )

    assert res.status_code == 200
    data = res.json()
    assert data["outcome"] == "report_warning"
    assert data["reports_updated"] is False
    assert "Simulated Disk Write Failure" not in data["reports_error"]  # Sanitized!
    assert "Report regeneration failed" in data["reports_error"]
    assert "Data saved to database" in data["message"]
    assert "Data is preserved in ledger" in data["message"]

    # CRITICAL CHECK: Verify lead WAS committed into SQLite despite report generation failure
    with sqlite3.connect(str(db_path)) as conn:
        lead_row = conn.execute("SELECT COUNT(*) FROM leads WHERE external_ref = 'POL-RES-01'").fetchone()
        assert lead_row[0] == 1, "Lead must be committed in database even when report generation fails!"


# ==============================================================================
# Scenario 8: Combined bundle upload with mixed outcomes
# ==============================================================================
def test_scenario_8a_bundle_pre_validation_failure(upload_test_env):
    """Bundle pre-validation failure (bad events extension) rejects before DB ingestion."""
    client, db_path, _, _ = upload_test_env

    valid_leads = (
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        "Bundle Lead,9876547771,POL-B-01,2026-12-31,10000,Car,en\n"
    ).encode("utf-8")
    invalid_events = b"some random text\n"

    res = client.post(
        "/api/upload/bundle",
        data={"workflow_id": "motor_renewal"},
        files={
            "leads_file": ("leads.csv", io.BytesIO(valid_leads), "text/csv"),
            "events_file": ("events.txt", io.BytesIO(invalid_events), "text/plain"),  # Invalid extension
        },
    )
    assert res.status_code == 400
    assert "invalid events file extension" in res.json()["detail"].lower()

    # Verify no leads were inserted
    with sqlite3.connect(str(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0


def test_scenario_8b_bundle_mixed_outcome_warnings(upload_test_env):
    """Bundle where leads succeed partially and events succeed partially returns bundle_warning."""
    client, db_path, _, _ = upload_test_env

    # Leads: 1 valid, 1 missing name (partial leads)
    leads_csv = (
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        "Lead Good,9876548881,POL-BM-01,2026-12-31,10000,Car,en\n"
        ",9876548882,POL-BM-02,2026-12-31,12000,Car,en\n"
    ).encode("utf-8")

    # Events: 1 valid event, 1 malformed line
    events_jsonl = (
        '{"event_id": "ev_b_01", "workflow_id": "motor_renewal", "event_type": "call.initiated", "call_id": "c_b_01", "occurred_at": "2026-11-01T10:00:00Z", "phone": "9876548881"}\n'
        'invalid non-json line\n'
    ).encode("utf-8")

    res = client.post(
        "/api/upload/bundle",
        data={"workflow_id": "motor_renewal"},
        files={
            "leads_file": ("bundle_leads.csv", io.BytesIO(leads_csv), "text/csv"),
            "events_file": ("bundle_events.jsonl", io.BytesIO(events_jsonl), "application/jsonl"),
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["outcome"] == "bundle_warning"
    assert data["leads"]["outcome"] == "partial_success"
    assert data["events"]["outcome"] == "partial_success"
    assert data["leads"]["metrics"]["lead_rows_accepted"] == 1
    assert data["leads"]["metrics"]["lead_rows_rejected"] == 1
    assert data["events"]["metrics"]["events_processed"] == 1
    assert data["events"]["metrics"]["malformed_events"] == 1
    assert "Bundle processed with warnings" in data["message"]
