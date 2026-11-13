"""Comprehensive verification tests for Dashboard Metrics and Data Quality Consistency (Task 4).

Verifies:
1. Reconciliation identity:
   Total Input Rows == Unique Accepted Leads + Duplicate Rows Merged + Rejected Rows
2. Empty database behavior (zeroes across all 4 dimensions).
3. Single upload with valid leads and schema rejections.
4. Subsequent duplicate ingestion (deduplication updates existing leads, increments duplicate_lead_rows_merged).
5. Multiple workflows (partitioned by workflow_id, canonical_phone).
6. Cross-artifact consistency with SQLite tables, batch_summary.csv, lead_report.csv, rejections.csv, and data_quality.json.
7. Exact preservation of the 10-key data_quality.json schema.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from solution.db import get_db_connection
from solution.web.app import create_app


@pytest.fixture
def test_env(tmp_path: Path):
    """Provide isolated app and storage directories for metrics testing."""
    db_path = tmp_path / "metrics_test.db"
    reports_dir = tmp_path / "reports"
    uploads_dir = tmp_path / "uploads"
    app = create_app(db_path=db_path, reports_dir=reports_dir, uploads_dir=uploads_dir)
    client = TestClient(app)
    return client, db_path, reports_dir, uploads_dir


def test_empty_database_metrics_baseline(test_env):
    """Verify all 4 metrics are cleanly zero on a fresh database."""
    client, db_path, reports_dir, _ = test_env

    res = client.get("/api/dashboard/stats")
    assert res.status_code == 200
    s = res.json()["summary"]

    assert s["input_rows_processed"] == 0
    assert s["unique_leads"] == 0
    assert s["duplicate_lead_rows_merged"] == 0
    assert s["rejected_leads"] == 0
    assert s["total_leads"] == 0
    assert s["accepted_leads"] == 0


def test_single_upload_reconciliation(test_env):
    """Verify reconciliation identity on first upload containing valid and invalid rows.

    Upload 5 rows:
    - 3 valid leads
    - 2 invalid rows (1 invalid phone, 1 missing customer name)
    """
    client, db_path, reports_dir, _ = test_env

    csv_data = (
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        "Aarav Sharma,9876540001,POL-001,2026-12-31,15000,Honda City,en\n"
        "Neha Gupta,9876540002,POL-002,2026-12-31,18000,Hyundai Creta,hi\n"
        "Bad Phone Lead,12345,POL-003,2026-12-31,12000,Maruti Swift,en\n"
        ",9876540004,POL-004,2026-12-31,10000,Tata Nexon,en\n"  # missing customer name
        "Karan Malhotra,9876540005,POL-005,2026-12-31,22000,Kia Seltos,en\n"
    ).encode("utf-8")

    res = client.post(
        "/api/upload/leads",
        data={"workflow_id": "motor_renewal"},
        files={"file": ("batch_1.csv", io.BytesIO(csv_data), "text/csv")},
    )
    assert res.status_code == 200

    # Query Dashboard Stats
    stats = client.get("/api/dashboard/stats").json()["summary"]
    input_rows = stats["input_rows_processed"]
    unique_leads = stats["unique_leads"]
    dup_merged = stats["duplicate_lead_rows_merged"]
    rejected = stats["rejected_leads"]

    assert input_rows == 5
    assert unique_leads == 3
    assert rejected == 2
    assert dup_merged == 0

    # Core mathematical identity check:
    assert input_rows == unique_leads + dup_merged + rejected

    # Cross-check with data_quality.json report
    dq = json.loads((reports_dir / "data_quality.json").read_text(encoding="utf-8"))
    assert dq["lead_rows_total"] == 5
    assert dq["lead_rows_rejected"] == 2
    assert dq["duplicate_lead_rows_merged"] == 0

    # Cross-check with lead_report.csv
    lead_rows = list(csv.DictReader((reports_dir / "lead_report.csv").read_text(encoding="utf-8").splitlines()))
    assert len(lead_rows) == 3

    # Cross-check with rejections.csv
    rej_rows = list(csv.DictReader((reports_dir / "rejections.csv").read_text(encoding="utf-8").splitlines()))
    assert len(rej_rows) == 2


def test_duplicate_upload_and_reconciliation_identity(test_env):
    """Verify that duplicate rows increment duplicate_lead_rows_merged and balance formula.

    1. First upload: 3 valid leads.
    2. Second upload: 2 duplicate leads (existing phones) + 2 new valid leads.
    Total input rows: 3 + 4 = 7.
    Total unique leads: 3 + 2 = 5.
    Total duplicates merged: 2.
    Total rejected: 0.
    Identity: 7 == 5 + 2 + 0.
    """
    client, db_path, reports_dir, _ = test_env

    csv_batch1 = (
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        "Lead One,9876541001,POL-101,2026-12-31,10000,Model A,en\n"
        "Lead Two,9876541002,POL-102,2026-12-31,12000,Model B,en\n"
        "Lead Three,9876541003,POL-103,2026-12-31,14000,Model C,en\n"
    ).encode("utf-8")

    client.post(
        "/api/upload/leads",
        data={"workflow_id": "motor_renewal"},
        files={"file": ("batch_a.csv", io.BytesIO(csv_batch1), "text/csv")},
    )

    csv_batch2 = (
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        "Lead One Updated,9876541001,POL-101,2026-12-31,11000,Model A,hi\n"  # Duplicate of Lead One
        "Lead Two Updated,9876541002,POL-102,2026-12-31,13000,Model B,en\n"  # Duplicate of Lead Two
        "Lead Four,9876541004,POL-104,2026-12-31,16000,Model D,en\n"        # New unique
        "Lead Five,9876541005,POL-105,2026-12-31,18000,Model E,en\n"        # New unique
    ).encode("utf-8")

    client.post(
        "/api/upload/leads",
        data={"workflow_id": "motor_renewal"},
        files={"file": ("batch_b.csv", io.BytesIO(csv_batch2), "text/csv")},
    )

    stats = client.get("/api/dashboard/stats").json()["summary"]
    input_rows = stats["input_rows_processed"]
    unique_leads = stats["unique_leads"]
    dup_merged = stats["duplicate_lead_rows_merged"]
    rejected = stats["rejected_leads"]

    assert input_rows == 7
    assert unique_leads == 5
    assert dup_merged == 2
    assert rejected == 0

    # Core mathematical identity check:
    assert input_rows == unique_leads + dup_merged + rejected

    # Verify SQLite database directly
    with get_db_connection(db_path) as conn:
        db_leads_count = conn.execute("SELECT COUNT(*) as c FROM leads").fetchone()["c"]
        assert db_leads_count == 5

    # Verify reports
    dq = json.loads((reports_dir / "data_quality.json").read_text(encoding="utf-8"))
    assert dq["lead_rows_total"] == 7
    assert dq["duplicate_lead_rows_merged"] == 2
    assert dq["lead_rows_rejected"] == 0

    lead_rep = list(csv.DictReader((reports_dir / "lead_report.csv").read_text(encoding="utf-8").splitlines()))
    assert len(lead_rep) == 5


def test_multiple_workflows_isolation_and_aggregation(test_env):
    """Verify metrics across motor_renewal and policy_feedback workflows."""
    client, db_path, reports_dir, _ = test_env

    # 1. motor_renewal: 2 rows (1 valid, 1 rejected)
    csv_mr = (
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        "Vikram Seth,9876542001,POL-MR-1,2026-12-31,15000,Honda City,en\n"
        "Bad Phone,999,POL-MR-2,2026-12-31,12000,Honda City,en\n"
    ).encode("utf-8")

    client.post(
        "/api/upload/leads",
        data={"workflow_id": "motor_renewal"},
        files={"file": ("mr.csv", io.BytesIO(csv_mr), "text/csv")},
    )

    # 2. policy_feedback: 2 rows (both valid, same phone as mr valid lead - distinct lead per workflow)
    csv_pf = (
        "Customer Name,Phone Number,Order ID,Product,Preferred Language\n"
        "Vikram Seth,9876542001,ORD-PF-1,Smartphone,en\n"
        "Anand Kumar,9876542002,ORD-PF-2,Laptop,en\n"
    ).encode("utf-8")

    client.post(
        "/api/upload/leads",
        data={"workflow_id": "policy_feedback"},
        files={"file": ("pf.csv", io.BytesIO(csv_pf), "text/csv")},
    )

    stats = client.get("/api/dashboard/stats").json()["summary"]
    assert stats["input_rows_processed"] == 4
    assert stats["unique_leads"] == 3
    assert stats["rejected_leads"] == 1
    assert stats["duplicate_lead_rows_merged"] == 0
    assert stats["input_rows_processed"] == stats["unique_leads"] + stats["duplicate_lead_rows_merged"] + stats["rejected_leads"]

    # Verify batch_summary.csv sum of total_leads matches unique_leads exactly
    batch_rows = list(csv.DictReader((reports_dir / "batch_summary.csv").read_text(encoding="utf-8").splitlines()))
    batch_sum = sum(int(b["total_leads"]) for b in batch_rows)
    assert batch_sum == 3


def test_data_quality_report_exact_schema_preservation(test_env):
    """Requirement 4: Verify the 10-key contract of data_quality.json is preserved exactly."""
    client, db_path, reports_dir, _ = test_env

    # Upload lead and event
    csv_leads = (
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        "Sunil Roy,9876543001,POL-101,2026-12-31,10000,Car,en\n"
    ).encode("utf-8")
    client.post("/api/upload/leads", data={"workflow_id": "motor_renewal"}, files={"file": ("leads.csv", io.BytesIO(csv_leads), "text/csv")})

    dq_path = reports_dir / "data_quality.json"
    assert dq_path.exists()
    dq = json.loads(dq_path.read_text(encoding="utf-8"))

    expected_keys = {
        "lead_rows_total",
        "lead_rows_rejected",
        "rejections_by_reason",
        "duplicate_lead_rows_merged",
        "field_warnings",
        "events_total",
        "malformed_events",
        "duplicate_events_dropped",
        "orphan_events",
        "unknown_disposition_events",
    }
    assert set(dq.keys()) == expected_keys


def test_repeated_upload_and_failed_upload_resilience(test_env):
    """Condition 2: Confirm file_metrics records each upload and prevents double-counting on failures."""
    client, db_path, reports_dir, _ = test_env

    valid_csv = (
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        "Same Customer,9876549001,POL-REP-01,2026-12-31,10000,Model A,en\n"
        "Another Customer,9876549002,POL-REP-02,2026-12-31,12000,Model B,en\n"
    ).encode("utf-8")

    # 1. First upload
    r1 = client.post("/api/upload/leads", data={"workflow_id": "motor_renewal"}, files={"file": ("repeat.csv", io.BytesIO(valid_csv), "text/csv")})
    assert r1.status_code == 200

    stats1 = client.get("/api/dashboard/stats").json()["summary"]
    assert stats1["input_rows_processed"] == 2
    assert stats1["unique_leads"] == 2
    assert stats1["duplicate_lead_rows_merged"] == 0

    # 2. Second upload with identical file (retry/re-upload prevents double-counting)
    r2 = client.post("/api/upload/leads", data={"workflow_id": "motor_renewal"}, files={"file": ("repeat.csv", io.BytesIO(valid_csv), "text/csv")})
    assert r2.status_code == 200

    stats2 = client.get("/api/dashboard/stats").json()["summary"]
    # Checksum idempotency ensures file_metrics does not double-count retried uploads
    assert stats2["input_rows_processed"] == 2
    assert stats2["unique_leads"] == 2
    assert stats2["duplicate_lead_rows_merged"] == 0
    assert stats2["rejected_leads"] == 0
    assert stats2["input_rows_processed"] == stats2["unique_leads"] + stats2["duplicate_lead_rows_merged"] + stats2["rejected_leads"]

    # 3. Failed upload attempt (invalid headers / bad schema)
    bad_csv = "WrongHeader1,WrongHeader2\nValue1,Value2\n".encode("utf-8")
    r_bad = client.post("/api/upload/leads", data={"workflow_id": "motor_renewal"}, files={"file": ("corrupt.csv", io.BytesIO(bad_csv), "text/csv")})
    assert r_bad.status_code == 400

    # Verify metrics remain completely untouched after failed upload
    stats3 = client.get("/api/dashboard/stats").json()["summary"]
    assert stats3["input_rows_processed"] == 2
    assert stats3["unique_leads"] == 2
    assert stats3["duplicate_lead_rows_merged"] == 0
    assert stats3["rejected_leads"] == 0
