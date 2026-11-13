"""Tests for Stage 7: Report downloads, Excel compatibility, JSON validity, and processing feedback."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from solution.web.app import create_app
from solution.web.config import ALLOWED_REPORT_FILES


@pytest.fixture
def test_env(tmp_path: Path):
    """Provide an isolated environment with temporary database and reports directory."""
    db_path = tmp_path / "stage7_test.db"
    reports_dir = tmp_path / "reports"
    uploads_dir = tmp_path / "uploads"
    app = create_app(db_path=db_path, reports_dir=reports_dir, uploads_dir=uploads_dir)
    client = TestClient(app)
    return client, db_path, reports_dir, uploads_dir


def test_all_five_report_downloads_and_headers(test_env):
    """Verify all five official reports are downloadable with security headers and correct MIME types."""
    client, _, reports_dir, _ = test_env

    # Seed data
    csv_leads = (
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        "Aarav Sharma,9876543001,POL-001,2025-12-31,18000,Honda City,en\n"
    ).encode("utf-8")
    client.post(
        "/api/upload/leads",
        data={"workflow_id": "motor_renewal"},
        files={"file": ("leads.csv", io.BytesIO(csv_leads), "text/csv")},
    )

    for filename in ALLOWED_REPORT_FILES:
        resp = client.get(f"/api/reports/download/{filename}")
        assert resp.status_code == 200, f"Failed downloading {filename}"
        assert "no-cache" in resp.headers.get("cache-control", "").lower()
        assert filename in resp.headers.get("content-disposition", "")

        if filename.endswith(".csv"):
            assert "text/csv" in resp.headers.get("content-type", "")
            assert "charset=utf-8" in resp.headers.get("content-type", "").lower()
        elif filename.endswith(".json"):
            assert "application/json" in resp.headers.get("content-type", "")


def test_csv_downloads_excel_compatibility(test_env):
    """Ensure CSV downloads are valid RFC 4180 CSVs that parse without syntax errors."""
    client, _, _, _ = test_env

    csv_leads = (
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        "Aarav Sharma,9876543001,POL-001,2025-12-31,18000,Honda City,en\n"
        "Priya Patel,9876543002,POL-002,2026-06-30,22000,Hyundai Creta,hi\n"
        "Bad Row,invalid,POL-003,2026-06-30,22000,Hyundai Creta,hi\n"
    ).encode("utf-8")
    client.post(
        "/api/upload/leads",
        data={"workflow_id": "motor_renewal"},
        files={"file": ("leads.csv", io.BytesIO(csv_leads), "text/csv")},
    )

    csv_files = ["batch_summary.csv", "daily_overview.csv", "lead_report.csv", "rejections.csv"]
    for csv_file in csv_files:
        resp = client.get(f"/api/reports/download/{csv_file}")
        assert resp.status_code == 200
        # Verify it can be cleanly parsed as CSV
        reader = csv.reader(io.StringIO(resp.text))
        rows = list(reader)
        assert len(rows) >= 1  # Header present
        header = rows[0]
        assert len(header) > 0


def test_json_download_validity_and_schema(test_env):
    """Ensure data_quality.json download is valid JSON and contains all 10 specification keys."""
    client, _, _, _ = test_env

    csv_leads = (
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        "Aarav Sharma,9876543001,POL-001,2025-12-31,18000,Honda City,en\n"
    ).encode("utf-8")
    client.post(
        "/api/upload/leads",
        data={"workflow_id": "motor_renewal"},
        files={"file": ("leads.csv", io.BytesIO(csv_leads), "text/csv")},
    )

    resp = client.get("/api/reports/download/data_quality.json")
    assert resp.status_code == 200
    data = json.loads(resp.text)

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
    assert expected_keys.issubset(set(data.keys()))


def test_reports_status_metadata_accuracy(test_env):
    """Verify /api/reports/status reports correct row counts, sizes, and timestamps."""
    client, _, _, _ = test_env

    csv_leads = (
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        "Aarav Sharma,9876543001,POL-001,2025-12-31,18000,Honda City,en\n"
        "Bad Row,bad_phone,POL-002,2026-01-01,10000,Model,en\n"
    ).encode("utf-8")
    client.post(
        "/api/upload/leads",
        data={"workflow_id": "motor_renewal"},
        files={"file": ("leads.csv", io.BytesIO(csv_leads), "text/csv")},
    )

    resp = client.get("/api/reports/status")
    assert resp.status_code == 200
    reports = {r["filename"]: r for r in resp.json()["reports"]}

    assert reports["lead_report.csv"]["exists"] is True
    assert reports["lead_report.csv"]["row_count"] == 1
    assert reports["lead_report.csv"]["size_bytes"] > 0
    assert reports["lead_report.csv"]["modified_at"] is not None

    assert reports["rejections.csv"]["exists"] is True
    assert reports["rejections.csv"]["row_count"] == 1


def test_no_internal_paths_or_sensitive_info_in_downloads(test_env):
    """Verify report downloads and status endpoints never leak server filesystem paths."""
    client, _, _, _ = test_env

    status_resp = client.get("/api/reports/status")
    assert "Desktop" not in status_resp.text
    assert "AppData" not in status_resp.text
    assert "Users" not in status_resp.text

    for filename in ALLOWED_REPORT_FILES:
        dl_resp = client.get(f"/api/reports/download/{filename}")
        assert "Desktop" not in dl_resp.headers.get("content-disposition", "")
        assert "/" not in dl_resp.headers.get("content-disposition", "").split("filename=")[-1]
        assert "\\" not in dl_resp.headers.get("content-disposition", "")


def test_bundle_upload_feedback_metrics(test_env):
    """Verify bundle upload returns comprehensive counts for both leads and events."""
    client, _, _, _ = test_env

    csv_leads = (
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        "Aarav Sharma,9876543001,POL-001,2025-12-31,18000,Honda City,en\n"
    ).encode("utf-8")

    events_jsonl = (
        '{"event_id":"evt-1","workflow_id":"motor_renewal","event_type":"call.initiated","call_id":"CALL-1","occurred_at":"2026-11-01T10:00:00Z","phone":"9876543001"}\n'
        '{"event_id":"evt-orphan","workflow_id":"motor_renewal","event_type":"call.initiated","call_id":"CALL-2","occurred_at":"2026-11-01T10:00:00Z","phone":"9999999999"}\n'
    ).encode("utf-8")

    resp = client.post(
        "/api/upload/bundle",
        data={"workflow_id": "motor_renewal"},
        files={
            "leads_file": ("leads.csv", io.BytesIO(csv_leads), "text/csv"),
            "events_file": ("events.jsonl", io.BytesIO(events_jsonl), "application/x-jsonlines"),
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert data["leads"]["metrics"]["lead_rows_accepted"] == 1
    assert data["events"]["metrics"]["events_processed"] == 2
    assert data["events"]["metrics"]["active_orphan_events"] == 1
