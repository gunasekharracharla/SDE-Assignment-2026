"""Tests for web dashboard endpoints, search/pagination, ledger queries, reports status/download, and frontend root."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from solution.db import get_db_connection
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
    assert stats["summary"]["unique_leads"] == 2
    assert stats["summary"]["accepted_leads"] == 2
    assert stats["summary"]["total_leads"] == 2
    assert stats["summary"]["rejected_leads"] == 1
    assert stats["summary"]["input_rows_processed"] == 3
    assert stats["summary"]["duplicate_lead_rows_merged"] == 0
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


def test_get_lead_calls_endpoint(test_client: TestClient):
    """Verify GET /api/dashboard/leads/{lead_id}/calls returns calls and workflow disposition catalogue."""
    # 1. Non-existent lead returns 404
    r_not_found = test_client.get("/api/dashboard/leads/99999/calls")
    assert r_not_found.status_code == 404

    # 2. Ingest lead with no calls yet
    csv_leads = (
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        "Aarav Verma,9876543100,POL-OVR-01,2026-12-31,15000,Honda City,en\n"
    )
    r_lead = test_client.post(
        "/api/upload/leads",
        data={"workflow_id": "motor_renewal"},
        files={"file": ("leads.csv", io.BytesIO(csv_leads.encode("utf-8")), "text/csv")},
    )
    assert r_lead.status_code == 200
    lead_id = test_client.get("/api/dashboard/leads").json()["leads"][0]["id"]

    # Query calls when lead has 0 calls
    r_zero_calls = test_client.get(f"/api/dashboard/leads/{lead_id}/calls")
    assert r_zero_calls.status_code == 200
    data_zero = r_zero_calls.json()
    assert data_zero["status"] == "success"
    assert data_zero["calls"] == []
    assert len(data_zero["allowed_dispositions"]) > 0

    # 3. Ingest call events for this lead
    events = (
        '{"event_id":"evt-o1","workflow_id":"motor_renewal","event_type":"call.initiated","call_id":"CALL-OVR-1","occurred_at":"2026-03-02T10:00:00Z","phone":"9876543100"}\n'
        '{"event_id":"evt-o2","workflow_id":"motor_renewal","event_type":"call.connected","call_id":"CALL-OVR-1","occurred_at":"2026-03-02T10:01:00Z","phone":"9876543100"}\n'
        '{"event_id":"evt-o3","workflow_id":"motor_renewal","event_type":"disposition.set","call_id":"CALL-OVR-1","occurred_at":"2026-03-02T10:05:00Z","payload":{"disposition_code":"callback_requested"}}\n'
    )
    test_client.post(
        "/api/upload/events",
        files={"file": ("events.jsonl", io.BytesIO(events.encode("utf-8")), "application/x-jsonlines")},
    )

    # 4. Query calls again
    r_with_calls = test_client.get(f"/api/dashboard/leads/{lead_id}/calls")
    assert r_with_calls.status_code == 200
    data_calls = r_with_calls.json()
    assert len(data_calls["calls"]) == 1
    call = data_calls["calls"][0]
    assert call["call_id"] == "CALL-OVR-1"
    assert call["is_connected"] is True
    assert call["effective_disposition"] == "callback_requested"
    assert call["is_terminal"] is False
    assert call["is_success"] is False

    # Check allowed dispositions match motor_renewal schema
    codes = [d["code"] for d in data_calls["allowed_dispositions"]]
    assert "renewed" in codes
    assert "callback_requested" in codes
    assert "not_interested" in codes


def test_post_override_validation_errors(test_client: TestClient):
    """Verify input validation, non-existent leads/calls, and duplicate prevention for override endpoint."""
    # Ingest lead and call
    csv_leads = (
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        "Sunil Rao,9876543200,POL-OVR-02,2026-12-31,18000,Maruti Brezza,en\n"
    )
    test_client.post(
        "/api/upload/leads",
        data={"workflow_id": "motor_renewal"},
        files={"file": ("leads.csv", io.BytesIO(csv_leads.encode("utf-8")), "text/csv")},
    )
    lead_id = test_client.get("/api/dashboard/leads").json()["leads"][0]["id"]

    events = (
        '{"event_id":"evt-s1","workflow_id":"motor_renewal","event_type":"call.initiated","call_id":"CALL-SUNIL-1","occurred_at":"2026-03-02T10:00:00Z","phone":"9876543200"}\n'
        '{"event_id":"evt-s2","workflow_id":"motor_renewal","event_type":"disposition.set","call_id":"CALL-SUNIL-1","occurred_at":"2026-03-02T10:05:00Z","payload":{"disposition_code":"callback_requested"}}\n'
    )
    test_client.post(
        "/api/upload/events",
        files={"file": ("events.jsonl", io.BytesIO(events.encode("utf-8")), "application/x-jsonlines")},
    )

    # 1. Non-existent lead_id -> 404
    r_bad_lead = test_client.post(
        "/api/dashboard/calls/override",
        json={"lead_id": 99999, "call_id": "CALL-SUNIL-1", "disposition_code": "renewed"},
    )
    assert r_bad_lead.status_code == 404

    # 2. Blank call_id -> 400
    r_blank_call = test_client.post(
        "/api/dashboard/calls/override",
        json={"lead_id": lead_id, "call_id": "   ", "disposition_code": "renewed"},
    )
    assert r_blank_call.status_code == 400

    # 3. Call ID not associated with lead -> 400
    r_wrong_call = test_client.post(
        "/api/dashboard/calls/override",
        json={"lead_id": lead_id, "call_id": "CALL-UNKNOWN-99", "disposition_code": "renewed"},
    )
    assert r_wrong_call.status_code == 400
    assert "does not belong to lead" in r_wrong_call.json()["detail"]

    # 4. Invalid disposition code not in motor_renewal schema -> 400
    r_bad_disp = test_client.post(
        "/api/dashboard/calls/override",
        json={"lead_id": lead_id, "call_id": "CALL-SUNIL-1", "disposition_code": "made_up_outcome"},
    )
    assert r_bad_disp.status_code == 400
    assert "not valid for workflow" in r_bad_disp.json()["detail"]

    # 5. Reason exceeding 500 characters -> 400 or 422
    r_long_reason = test_client.post(
        "/api/dashboard/calls/override",
        json={"lead_id": lead_id, "call_id": "CALL-SUNIL-1", "disposition_code": "renewed", "reason": "A" * 501},
    )
    assert r_long_reason.status_code in (400, 422)

    # 6. Duplicate active override prevention
    # Apply override first
    r_first = test_client.post(
        "/api/dashboard/calls/override",
        json={"lead_id": lead_id, "call_id": "CALL-SUNIL-1", "disposition_code": "renewed", "reason": "Customer agreed to renew"},
    )
    assert r_first.status_code == 200

    # Apply same override immediately
    r_dup = test_client.post(
        "/api/dashboard/calls/override",
        json={"lead_id": lead_id, "call_id": "CALL-SUNIL-1", "disposition_code": "renewed", "reason": "Duplicate submission attempt"},
    )
    assert r_dup.status_code == 400
    assert "already has active override" in r_dup.json()["detail"]


def test_post_override_success_and_append_only(test_client: TestClient, app_and_dirs):
    """Verify successful override, append-only ledger immutability, and schema-compliant audit metadata."""
    app, db_path, reports_dir, _ = app_and_dirs

    # Setup lead and call
    csv_leads = (
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        "Meera Nair,9876543300,POL-OVR-03,2026-12-31,22000,Kia Seltos,en\n"
    )
    test_client.post(
        "/api/upload/leads",
        data={"workflow_id": "motor_renewal"},
        files={"file": ("leads.csv", io.BytesIO(csv_leads.encode("utf-8")), "text/csv")},
    )
    lead_id = test_client.get("/api/dashboard/leads").json()["leads"][0]["id"]

    events = (
        '{"event_id":"evt-m1","workflow_id":"motor_renewal","event_type":"call.initiated","call_id":"CALL-MEERA-1","occurred_at":"2026-03-02T10:00:00Z","phone":"9876543300"}\n'
        '{"event_id":"evt-m2","workflow_id":"motor_renewal","event_type":"call.connected","call_id":"CALL-MEERA-1","occurred_at":"2026-03-02T10:01:00Z","phone":"9876543300"}\n'
        '{"event_id":"evt-m3","workflow_id":"motor_renewal","event_type":"disposition.set","call_id":"CALL-MEERA-1","occurred_at":"2026-03-02T10:05:00Z","payload":{"disposition_code":"callback_requested"}}\n'
    )
    test_client.post(
        "/api/upload/events",
        files={"file": ("events.jsonl", io.BytesIO(events.encode("utf-8")), "application/x-jsonlines")},
    )

    with get_db_connection(db_path) as conn:
        initial_events = conn.execute("SELECT * FROM interaction_ledger ORDER BY rowid ASC").fetchall()
        assert len(initial_events) == 3

    # Post override
    reason_text = "Customer called back to complete payment and confirmed policy renewal"
    r_override = test_client.post(
        "/api/dashboard/calls/override",
        json={
            "lead_id": lead_id,
            "call_id": "CALL-MEERA-1",
            "disposition_code": "renewed",
            "reason": reason_text,
        },
    )
    assert r_override.status_code == 200
    res_data = r_override.json()
    assert res_data["status"] == "success"
    assert res_data["call_id"] == "CALL-MEERA-1"
    assert res_data["effective_disposition"] == "renewed"
    assert res_data["is_terminal"] is True
    assert res_data["is_success"] is True
    assert res_data["reports_updated"] is True

    # Verify append-only property in DB
    with get_db_connection(db_path) as conn:
        updated_events = conn.execute("SELECT * FROM interaction_ledger ORDER BY rowid ASC").fetchall()
        assert len(updated_events) == 4

        # Verify initial 3 events were NOT modified in any way
        for i in range(3):
            assert updated_events[i]["event_id"] == initial_events[i]["event_id"]
            assert updated_events[i]["event_type"] == initial_events[i]["event_type"]
            assert updated_events[i]["disposition_code"] == initial_events[i]["disposition_code"]
            assert updated_events[i]["occurred_at_utc"] == initial_events[i]["occurred_at_utc"]

        # Verify the 4th event is the override event
        ovr_event = updated_events[3]
        assert ovr_event["event_type"] == "disposition.override"
        assert ovr_event["call_id"] == "CALL-MEERA-1"
        assert ovr_event["disposition_code"] == "renewed"
        assert ovr_event["lead_id"] == lead_id
        assert ovr_event["is_orphan"] == 0

        # Verify payload_json contains audit_reason and source
        payload = json.loads(ovr_event["payload_json"])
        assert payload["disposition_code"] == "renewed"
        assert payload["audit_reason"] == reason_text
        assert payload["source"] == "web_manual_override"


def test_post_override_multiple_overrides_and_precedence(test_client: TestClient):
    """Verify that multiple successive overrides correctly resolve precedence by timestamp."""
    csv_leads = (
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        "Kiran Bedi,9876543400,POL-OVR-04,2026-12-31,16000,Hyundai Creta,en\n"
    )
    test_client.post(
        "/api/upload/leads",
        data={"workflow_id": "motor_renewal"},
        files={"file": ("leads.csv", io.BytesIO(csv_leads.encode("utf-8")), "text/csv")},
    )
    lead_id = test_client.get("/api/dashboard/leads").json()["leads"][0]["id"]

    events = (
        '{"event_id":"evt-k1","workflow_id":"motor_renewal","event_type":"call.initiated","call_id":"CALL-KIRAN-1","occurred_at":"2026-03-02T10:00:00Z","phone":"9876543400"}\n'
        '{"event_id":"evt-k2","workflow_id":"motor_renewal","event_type":"disposition.set","call_id":"CALL-KIRAN-1","occurred_at":"2026-03-02T10:05:00Z","payload":{"disposition_code":"callback_requested"}}\n'
    )
    test_client.post(
        "/api/upload/events",
        files={"file": ("events.jsonl", io.BytesIO(events.encode("utf-8")), "application/x-jsonlines")},
    )

    # First override: not_interested (non-success terminal)
    r1 = test_client.post(
        "/api/dashboard/calls/override",
        json={"lead_id": lead_id, "call_id": "CALL-KIRAN-1", "disposition_code": "not_interested", "reason": "Customer said no"},
    )
    assert r1.status_code == 200
    assert r1.json()["effective_disposition"] == "not_interested"
    assert r1.json()["is_terminal"] is True
    assert r1.json()["is_success"] is False

    # Second override: renewed (success terminal)
    r2 = test_client.post(
        "/api/dashboard/calls/override",
        json={"lead_id": lead_id, "call_id": "CALL-KIRAN-1", "disposition_code": "renewed", "reason": "Customer changed mind and renewed"},
    )
    assert r2.status_code == 200
    assert r2.json()["effective_disposition"] == "renewed"
    assert r2.json()["is_terminal"] is True
    assert r2.json()["is_success"] is True

    # Verify calls endpoint returns latest override
    calls_res = test_client.get(f"/api/dashboard/leads/{lead_id}/calls")
    assert calls_res.json()["calls"][0]["effective_disposition"] == "renewed"


def test_post_override_reports_and_dashboard_accuracy(test_client: TestClient, app_and_dirs):
    """Verify override immediately updates dashboard statistics and generated report files."""
    app, db_path, reports_dir, _ = app_and_dirs

    csv_leads = (
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        "Deepak Gupta,9876543500,POL-OVR-05,2026-12-31,25000,Toyota Fortuner,en\n"
    )
    test_client.post(
        "/api/upload/leads",
        data={"workflow_id": "motor_renewal"},
        files={"file": ("leads.csv", io.BytesIO(csv_leads.encode("utf-8")), "text/csv")},
    )
    lead_id = test_client.get("/api/dashboard/leads").json()["leads"][0]["id"]

    events = (
        '{"event_id":"evt-d1","workflow_id":"motor_renewal","event_type":"call.initiated","call_id":"CALL-DEEPAK-1","occurred_at":"2026-03-02T10:00:00Z","phone":"9876543500"}\n'
        '{"event_id":"evt-d2","workflow_id":"motor_renewal","event_type":"disposition.set","call_id":"CALL-DEEPAK-1","occurred_at":"2026-03-02T10:05:00Z","payload":{"disposition_code":"callback_requested"}}\n'
    )
    test_client.post(
        "/api/upload/events",
        files={"file": ("events.jsonl", io.BytesIO(events.encode("utf-8")), "application/x-jsonlines")},
    )

    # Initial stats check: bucket is in_progress_dialed, success is 0
    stats_before = test_client.get("/api/dashboard/stats").json()
    assert stats_before["summary"]["successful_leads"] == 0
    assert stats_before["bucket_distribution"]["in_progress_dialed"] == 1
    assert stats_before["bucket_distribution"]["completed"] == 0

    # Initial lead_report.csv check
    lead_rep_path = reports_dir / "lead_report.csv"
    assert lead_rep_path.exists()
    rows_before = list(csv.DictReader(lead_rep_path.read_text(encoding="utf-8").splitlines()))
    assert rows_before[0]["bucket"] == "in_progress_dialed"
    assert rows_before[0]["current_disposition"] == "callback_requested"
    assert rows_before[0]["ever_success"] == "false"

    # Post override to renewed
    r_ovr = test_client.post(
        "/api/dashboard/calls/override",
        json={"lead_id": lead_id, "call_id": "CALL-DEEPAK-1", "disposition_code": "renewed", "reason": "Audit verification"},
    )
    assert r_ovr.status_code == 200

    # Updated stats check: bucket is completed, successful_leads is 1
    stats_after = test_client.get("/api/dashboard/stats").json()
    assert stats_after["summary"]["successful_leads"] == 1
    assert stats_after["bucket_distribution"]["in_progress_dialed"] == 0
    assert stats_after["bucket_distribution"]["completed"] == 1

    # Updated lead_report.csv check
    rows_after = list(csv.DictReader(lead_rep_path.read_text(encoding="utf-8").splitlines()))
    assert rows_after[0]["bucket"] == "completed"
    assert rows_after[0]["current_disposition"] == "renewed"
    assert rows_after[0]["ever_success"] == "true"


def test_post_override_report_failure_resilience(test_client: TestClient, app_and_dirs):
    """Verify that if report regeneration fails, the override event is still committed and reports_updated is False."""
    app, db_path, reports_dir, _ = app_and_dirs

    csv_leads = (
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        "Rohan Bose,9876543600,POL-OVR-06,2026-12-31,14000,Honda Amaze,en\n"
    )
    test_client.post(
        "/api/upload/leads",
        data={"workflow_id": "motor_renewal"},
        files={"file": ("leads.csv", io.BytesIO(csv_leads.encode("utf-8")), "text/csv")},
    )
    lead_id = test_client.get("/api/dashboard/leads").json()["leads"][0]["id"]

    events = (
        '{"event_id":"evt-r1","workflow_id":"motor_renewal","event_type":"call.initiated","call_id":"CALL-ROHAN-1","occurred_at":"2026-03-02T10:00:00Z","phone":"9876543600"}\n'
        '{"event_id":"evt-r2","workflow_id":"motor_renewal","event_type":"disposition.set","call_id":"CALL-ROHAN-1","occurred_at":"2026-03-02T10:05:00Z","payload":{"disposition_code":"callback_requested"}}\n'
    )
    test_client.post(
        "/api/upload/events",
        files={"file": ("events.jsonl", io.BytesIO(events.encode("utf-8")), "application/x-jsonlines")},
    )

    # Patch generate_all_reports to raise an error
    with patch("solution.web.routes.dashboard.generate_all_reports", side_effect=RuntimeError("Disk write simulated failure")):
        r = test_client.post(
            "/api/dashboard/calls/override",
            json={"lead_id": lead_id, "call_id": "CALL-ROHAN-1", "disposition_code": "renewed"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "success"
        assert data["reports_updated"] is False

    # Verify event was still safely committed to SQLite ledger
    with get_db_connection(db_path) as conn:
        ovr = conn.execute(
            "SELECT disposition_code FROM interaction_ledger WHERE call_id = 'CALL-ROHAN-1' AND event_type = 'disposition.override'"
        ).fetchone()
        assert ovr is not None
        assert ovr["disposition_code"] == "renewed"
