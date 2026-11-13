"""Tests for web dashboard endpoints, search/pagination, ledger queries, reports status/download, and frontend root."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from solution.web.app import create_app


@pytest.fixture
def app_and_dirs(tmp_path: Path):
    """Provide an isolated app with dedicated temporary database and directories."""
    db_path = tmp_path / "dash_test.db"
    reports_dir = tmp_path / "reports"
    uploads_dir = tmp_path / "uploads"
    app = create_app(db_path=db_path, reports_dir=reports_dir, uploads_dir=uploads_dir)
    return app, db_path, reports_dir, uploads_dir


@pytest.fixture
def test_client(app_and_dirs):
    """Provide a TestClient for testing."""
    app, _, _, _ = app_and_dirs
    return TestClient(app)


def test_dashboard_stats_empty_db(test_client: TestClient):
    """Verify /api/dashboard/stats returns clean zeros on empty database."""
    response = test_client.get("/api/dashboard/stats")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    summary = data["summary"]
    assert summary["total_leads"] == 0
    assert summary["accepted_leads"] == 0
    assert summary["rejected_leads"] == 0
    assert summary["total_calls"] == 0
    assert summary["connected_calls"] == 0
    assert summary["successful_leads"] == 0
    assert summary["active_orphan_events"] == 0
    assert data["bucket_distribution"]["yet_to_reach_out"] == 0


def test_dashboard_stats_and_leads_with_data(test_client: TestClient):
    """Upload valid lead and event data, verify stats, leads pagination, search, and masking."""
    csv_content = (
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        "Amit Kumar,9876543210,POL-1001,2026-12-31,15000,Honda City,en\n"
        "Priya Sharma,9876543211,POL-1002,2026-12-31,12000,Hyundai i20,hi\n"
        "Bad Record,12345,POL-1003,2026-12-31,10000,Maruti Swift,en\n"
    )
    res_leads = test_client.post(
        "/api/upload/leads",
        data={"workflow_id": "motor_renewal"},
        files={"file": ("test_leads.csv", io.BytesIO(csv_content.encode("utf-8")), "text/csv")},
    )
    assert res_leads.status_code == 200
    metrics = res_leads.json()["metrics"]
    assert metrics["lead_rows_accepted"] == 2
    assert metrics["lead_rows_rejected"] == 1

    jsonl_content = (
        '{"event_id":"evt-1","workflow_id":"motor_renewal","event_type":"call.initiated","call_id":"CALL-1","occurred_at":"2026-03-02T10:00:00Z","phone":"9876543210"}\n'
        '{"event_id":"evt-2","workflow_id":"motor_renewal","event_type":"call.connected","call_id":"CALL-1","occurred_at":"2026-03-02T10:01:00Z","phone":"9876543210"}\n'
        '{"event_id":"evt-3","workflow_id":"motor_renewal","event_type":"disposition.set","call_id":"CALL-1","occurred_at":"2026-03-02T10:05:00Z","payload":{"disposition_code":"renewed"}}\n'
    )
    res_events = test_client.post(
        "/api/upload/events",
        files={"file": ("test_events.jsonl", io.BytesIO(jsonl_content.encode("utf-8")), "application/x-jsonlines")},
    )
    assert res_events.status_code == 200

    # Test /api/dashboard/stats
    res_stats = test_client.get("/api/dashboard/stats")
    assert res_stats.status_code == 200
    stats = res_stats.json()
    assert stats["summary"]["accepted_leads"] == 2
    assert stats["summary"]["rejected_leads"] == 1
    assert stats["summary"]["total_calls"] == 1
    assert stats["summary"]["connected_calls"] == 1
    assert stats["summary"]["successful_leads"] == 1
    assert stats["bucket_distribution"]["completed"] == 1
    assert stats["bucket_distribution"]["yet_to_reach_out"] == 1
    assert len(stats["daily_activity"]) == 1
    assert stats["daily_activity"][0]["date"] == "2026-03-02"

    # Test /api/dashboard/leads list
    res_list = test_client.get("/api/dashboard/leads")
    assert res_list.status_code == 200
    leads_data = res_list.json()
    assert leads_data["total"] == 2
    assert len(leads_data["leads"]) == 2

    # Verify PII masking: No Amit Kumar, no Priya Sharma, no 9876543210
    leads_text = res_list.text
    assert "Amit Kumar" not in leads_text
    assert "Priya Sharma" not in leads_text
    assert "9876543210" not in leads_text
    assert "+91******3210" in leads_text
    assert "+91******3211" in leads_text

    # Test search by external_ref
    res_search = test_client.get("/api/dashboard/leads?q=POL-1001")
    assert res_search.status_code == 200
    assert res_search.json()["total"] == 1
    assert res_search.json()["leads"][0]["external_ref"] == "POL-1001"
    assert res_search.json()["leads"][0]["bucket"] == "completed"

    # Test search by masked phone substring
    res_search_phone = test_client.get("/api/dashboard/leads?q=3211")
    assert res_search_phone.status_code == 200
    assert res_search_phone.json()["total"] == 1
    assert res_search.json()["leads"][0]["external_ref"] == "POL-1001"

    # Test filter by bucket
    res_filter_completed = test_client.get("/api/dashboard/leads?bucket=completed")
    assert res_filter_completed.status_code == 200
    assert res_filter_completed.json()["total"] == 1
    assert res_filter_completed.json()["leads"][0]["external_ref"] == "POL-1001"

    res_filter_yet = test_client.get("/api/dashboard/leads?bucket=yet_to_reach_out")
    assert res_filter_yet.status_code == 200
    assert res_filter_yet.json()["total"] == 1
    assert res_filter_yet.json()["leads"][0]["external_ref"] == "POL-1002"


def test_dashboard_ledger_queries(test_client: TestClient):
    """Verify /api/dashboard/ledger endpoint, pagination, and zero PII."""
    csv_content = (
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        "Rajesh Patel,9876543210,POL-1001,2026-12-31,15000,Honda City,en\n"
    )
    test_client.post(
        "/api/upload/leads",
        data={"workflow_id": "motor_renewal"},
        files={"file": ("leads.csv", io.BytesIO(csv_content.encode("utf-8")), "text/csv")},
    )

    jsonl_content = (
        '{"event_id":"evt-1","workflow_id":"motor_renewal","event_type":"call.initiated","call_id":"CALL-1","occurred_at":"2026-03-02T10:00:00Z","phone":"9876543210"}\n'
        '{"event_id":"evt-2","workflow_id":"motor_renewal","event_type":"call.connected","call_id":"CALL-1","occurred_at":"2026-03-02T10:01:00Z","phone":"9876543210"}\n'
    )
    test_client.post(
        "/api/upload/events",
        files={"file": ("events.jsonl", io.BytesIO(jsonl_content.encode("utf-8")), "application/x-jsonlines")},
    )

    res_ledger = test_client.get("/api/dashboard/ledger")
    assert res_ledger.status_code == 200
    ledger_data = res_ledger.json()
    assert ledger_data["total"] == 2
    assert len(ledger_data["events"]) == 2

    # Check PII protection
    assert "9876543210" not in res_ledger.text
    assert "+91******3210" in res_ledger.text

    # Filter by event_type
    res_type_filter = test_client.get("/api/dashboard/ledger?event_type=call.connected")
    assert res_type_filter.status_code == 200
    assert res_type_filter.json()["total"] == 1
    assert res_type_filter.json()["events"][0]["event_id"] == "evt-2"


def test_reports_status_and_downloads(test_client: TestClient, app_and_dirs):
    """Verify /api/reports/status and /api/reports/download/{filename} routes with security checks."""
    app, _, reports_dir, _ = app_and_dirs

    csv_content = (
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        "Rajesh Patel,9876543210,POL-1001,2026-12-31,15000,Honda City,en\n"
    )
    test_client.post(
        "/api/upload/leads",
        data={"workflow_id": "motor_renewal"},
        files={"file": ("leads.csv", io.BytesIO(csv_content.encode("utf-8")), "text/csv")},
    )

    res_status = test_client.get("/api/reports/status")
    assert res_status.status_code == 200
    status_data = res_status.json()
    assert status_data["status"] == "success"
    filenames = [r["filename"] for r in status_data["reports"]]
    assert "batch_summary.csv" in filenames
    assert "daily_overview.csv" in filenames
    assert "lead_report.csv" in filenames
    assert "rejections.csv" in filenames
    assert "data_quality.json" in filenames

    # Test download of valid report
    res_dl = test_client.get("/api/reports/download/lead_report.csv")
    assert res_dl.status_code == 200
    assert "text/csv" in res_dl.headers["content-type"]
    assert "lead_report.csv" in res_dl.headers["content-disposition"]
    assert "external_ref" in res_dl.text

    # Test download of unauthorized or traversal file
    res_bad = test_client.get("/api/reports/download/../../etc/passwd")
    assert res_bad.status_code == 404

    res_unauth = test_client.get("/api/reports/download/database.db")
    assert res_unauth.status_code == 404


def test_frontend_root_serves_html(test_client: TestClient):
    """Verify GET / serves the SPA frontend HTML containing dashboard components."""
    response = test_client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    html = response.text
    assert "Interaction Ledger" in html
    assert "Upload Center" in html
    assert "Lead Management" in html
    assert "Official Reports" in html
    assert 'id="view-dashboard"' in html
    assert 'id="view-upload"' in html
    assert 'id="nav-dashboard"' in html
