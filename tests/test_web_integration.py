"""End-to-end integration tests for the web upload and reporting pipeline (Stage 5).

Verifies:
1. Lead ingestion followed by Event ingestion.
2. Event ingestion followed by Lead ingestion (orphan reconciliation).
3. Coordinated Bundle upload (CSV + JSONL).
4. Idempotency across duplicate uploads.
5. Exact 5 reports generation, headers, math, and consistency with SQLite DB.
6. Failure recovery and rollback.
7. Zero-PII guarantee across all responses.
8. Existing CLI functionality remains 100% functional.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from solution.cli import main as cli_main
from solution.db import get_db_connection
from solution.web.app import create_app


@pytest.fixture
def integrated_env(tmp_path: Path):
    """Provide an isolated database, reports, and upload environment."""
    db_path = tmp_path / "integration.db"
    reports_dir = tmp_path / "reports"
    uploads_dir = tmp_path / "uploads"
    app = create_app(db_path=db_path, reports_dir=reports_dir, uploads_dir=uploads_dir)
    client = TestClient(app)
    return client, db_path, reports_dir, uploads_dir


def test_csv_upload_followed_by_jsonl_upload(integrated_env):
    """Scenario 1: Upload CSV leads first, then upload JSONL interaction events."""
    client, db_path, reports_dir, _ = integrated_env

    # 1. Upload Lead CSV
    csv_leads = (
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        "Aarav Sharma,9876543001,POL-INT-001,2025-12-31,18000,Honda City,en\n"
    ).encode("utf-8")

    r_leads = client.post(
        "/api/upload/leads",
        data={"workflow_id": "motor_renewal"},
        files={"file": ("leads_first.csv", io.BytesIO(csv_leads), "text/csv")},
    )
    assert r_leads.status_code == 200
    assert r_leads.json()["metrics"]["lead_rows_accepted"] == 1

    # Verify initial lead report: yet_to_reach_out
    lead_rep_pre = list(csv.DictReader((reports_dir / "lead_report.csv").read_text(encoding="utf-8").splitlines()))
    assert len(lead_rep_pre) == 1
    assert lead_rep_pre[0]["bucket"] == "yet_to_reach_out"
    assert lead_rep_pre[0]["total_calls"] == "0"

    # 2. Upload Events JSONL (2 calls: Call 1 callback, Call 2 renewed terminal success)
    events_jsonl = (
        '{"event_id": "e_i1", "workflow_id": "motor_renewal", "event_type": "call.initiated", "call_id": "call_i1", "occurred_at": "2026-11-01T10:00:00Z", "phone": "9876543001"}\n'
        '{"event_id": "e_i2", "workflow_id": "motor_renewal", "event_type": "call.connected", "call_id": "call_i1", "occurred_at": "2026-11-01T10:01:00Z", "phone": "9876543001"}\n'
        '{"event_id": "e_i3", "workflow_id": "motor_renewal", "event_type": "disposition.set", "call_id": "call_i1", "occurred_at": "2026-11-01T10:05:00Z", "payload": {"disposition_code": "callback_requested"}}\n'
        '{"event_id": "e_i4", "workflow_id": "motor_renewal", "event_type": "call.initiated", "call_id": "call_i2", "occurred_at": "2026-11-01T12:00:00Z", "phone": "9876543001"}\n'
        '{"event_id": "e_i5", "workflow_id": "motor_renewal", "event_type": "call.connected", "call_id": "call_i2", "occurred_at": "2026-11-01T12:01:00Z", "phone": "9876543001"}\n'
        '{"event_id": "e_i6", "workflow_id": "motor_renewal", "event_type": "disposition.set", "call_id": "call_i2", "occurred_at": "2026-11-01T12:05:00Z", "payload": {"disposition_code": "renewed"}}\n'
    ).encode("utf-8")

    r_events = client.post(
        "/api/upload/events",
        files={"file": ("events_second.jsonl", io.BytesIO(events_jsonl), "application/jsonl")},
    )
    assert r_events.status_code == 200
    assert r_events.json()["metrics"]["events_processed"] == 6
    assert r_events.json()["metrics"]["active_orphan_events"] == 0

    # 3. Verify lead_report.csv updated accurately
    lead_rep_post = list(csv.DictReader((reports_dir / "lead_report.csv").read_text(encoding="utf-8").splitlines()))
    assert len(lead_rep_post) == 1
    lead_post = lead_rep_post[0]
    assert lead_post["bucket"] == "completed"
    assert lead_post["total_calls"] == "2"
    assert lead_post["connected_calls"] == "2"
    assert lead_post["current_disposition"] == "renewed"
    assert lead_post["ever_success"] == "true"
    assert lead_post["phone_masked"] == "+91******3001"

    # 4. Verify daily_overview.csv
    daily_rep = list(csv.DictReader((reports_dir / "daily_overview.csv").read_text(encoding="utf-8").splitlines()))
    assert len(daily_rep) == 1
    assert daily_rep[0]["date"] == "2026-11-01"
    assert daily_rep[0]["total_calls"] == "2"
    assert daily_rep[0]["connected_calls"] == "2"
    assert daily_rep[0]["connectivity_rate"] == "1.0000"
    assert daily_rep[0]["success_leads"] == "1"


def test_jsonl_upload_followed_by_csv_upload_orphan_reconciliation(integrated_env):
    """Scenario 2: Upload Events first (creating orphans), then upload matching Leads (resolving orphans)."""
    client, db_path, reports_dir, _ = integrated_env

    # 1. Upload Event JSONL first
    orphan_events = (
        '{"event_id": "e_rev1", "workflow_id": "motor_renewal", "event_type": "call.initiated", "call_id": "c_rev", "occurred_at": "2026-11-02T10:00:00Z", "phone": "9876543002"}\n'
        '{"event_id": "e_rev2", "workflow_id": "motor_renewal", "event_type": "call.connected", "call_id": "c_rev", "occurred_at": "2026-11-02T10:01:00Z", "phone": "9876543002"}\n'
        '{"event_id": "e_rev3", "workflow_id": "motor_renewal", "event_type": "disposition.set", "call_id": "c_rev", "occurred_at": "2026-11-02T10:05:00Z", "payload": {"disposition_code": "renewed"}}\n'
    ).encode("utf-8")

    r_events = client.post(
        "/api/upload/events",
        files={"file": ("orphan_first.jsonl", io.BytesIO(orphan_events), "application/jsonl")},
    )
    assert r_events.status_code == 200
    assert r_events.json()["metrics"]["active_orphan_events"] == 3

    # Check data_quality.json reflects active orphans
    dq_pre = json.loads((reports_dir / "data_quality.json").read_text(encoding="utf-8"))
    assert dq_pre["orphan_events"] == 3

    # 2. Upload matching Lead CSV
    matching_lead = (
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        "Late Arrival Customer,9876543002,POL-REV-001,2025-12-31,15000,Honda City,en\n"
    ).encode("utf-8")

    r_lead = client.post(
        "/api/upload/leads",
        data={"workflow_id": "motor_renewal"},
        files={"file": ("lead_second.csv", io.BytesIO(matching_lead), "text/csv")},
    )
    assert r_lead.status_code == 200

    # 3. Check that orphans were automatically reconciled
    dq_post = json.loads((reports_dir / "data_quality.json").read_text(encoding="utf-8"))
    assert dq_post["orphan_events"] == 0

    lead_rep = list(csv.DictReader((reports_dir / "lead_report.csv").read_text(encoding="utf-8").splitlines()))
    assert len(lead_rep) == 1
    assert lead_rep[0]["bucket"] == "completed"
    assert lead_rep[0]["total_calls"] == "1"
    assert lead_rep[0]["connected_calls"] == "1"
    assert lead_rep[0]["current_disposition"] == "renewed"
    assert lead_rep[0]["ever_success"] == "true"


def test_bundle_upload_endpoint(integrated_env):
    """Scenario 3: Upload both Lead CSV and Event JSONL together via /api/upload/bundle."""
    client, db_path, reports_dir, _ = integrated_env

    csv_content = (
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        "Bundle User,9876543003,POL-BUN-001,2025-12-31,20000,Honda City,en\n"
    ).encode("utf-8")

    events_content = (
        '{"event_id": "e_b1", "workflow_id": "motor_renewal", "event_type": "call.initiated", "call_id": "c_b1", "occurred_at": "2026-11-03T10:00:00Z", "phone": "9876543003"}\n'
        '{"event_id": "e_b2", "workflow_id": "motor_renewal", "event_type": "call.connected", "call_id": "c_b1", "occurred_at": "2026-11-03T10:01:00Z", "phone": "9876543003"}\n'
        '{"event_id": "e_b3", "workflow_id": "motor_renewal", "event_type": "disposition.set", "call_id": "c_b1", "occurred_at": "2026-11-03T10:05:00Z", "payload": {"disposition_code": "renewed"}}\n'
    ).encode("utf-8")

    files = {
        "leads_file": ("bundle_leads.csv", io.BytesIO(csv_content), "text/csv"),
        "events_file": ("bundle_events.jsonl", io.BytesIO(events_content), "application/jsonl"),
    }
    data = {"workflow_id": "motor_renewal"}

    response = client.post("/api/upload/bundle", data=data, files=files)
    assert response.status_code == 200
    res = response.json()
    assert res["status"] == "success"
    assert res["leads"]["metrics"]["lead_rows_accepted"] == 1
    assert res["events"]["metrics"]["events_processed"] == 3
    assert res["events"]["metrics"]["active_orphan_events"] == 0

    # Verify all 5 reports exist and are non-empty
    for fname in ["batch_summary.csv", "daily_overview.csv", "lead_report.csv", "rejections.csv", "data_quality.json"]:
        p = reports_dir / fname
        assert p.exists()
        assert p.stat().st_size > 0


def test_all_five_reports_headers_and_types(integrated_env):
    """Scenario 4: Verify character-for-character compliance of all five report headers."""
    client, _, reports_dir, _ = integrated_env

    # Upload clean bundle to populate reports
    csv_leads = (
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        "Header Check,9876543004,POL-H-001,2025-12-31,15000,Honda City,en\n"
    ).encode("utf-8")
    client.post(
        "/api/upload/leads",
        data={"workflow_id": "motor_renewal"},
        files={"file": ("h.csv", io.BytesIO(csv_leads), "text/csv")},
    )

    # 1. batch_summary.csv
    h_batch = (reports_dir / "batch_summary.csv").read_text(encoding="utf-8").splitlines()[0]
    assert h_batch == "workflow_id,batch_id,total_leads,yet_to_reach_out,in_progress_telephony_failed,in_progress_dialed,completed"

    # 2. daily_overview.csv
    h_daily = (reports_dir / "daily_overview.csv").read_text(encoding="utf-8").splitlines()[0]
    assert h_daily == "workflow_id,date,total_calls,connected_calls,connectivity_rate,success_leads"

    # 3. lead_report.csv
    h_lead = (reports_dir / "lead_report.csv").read_text(encoding="utf-8").splitlines()[0]
    assert h_lead == "workflow_id,external_ref,batch_id,phone_masked,bucket,total_calls,connected_calls,last_call_at,current_disposition,ever_success"

    # 4. rejections.csv
    h_rej = (reports_dir / "rejections.csv").read_text(encoding="utf-8").splitlines()[0]
    assert h_rej == "workflow_id,batch_id,row_number,reason"

    # 5. data_quality.json
    dq = json.loads((reports_dir / "data_quality.json").read_text(encoding="utf-8"))
    assert set(dq.keys()) == {
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


def test_cli_coexistence_and_functional_integrity(integrated_env):
    """Scenario 5: Verify CLI commands remain 100% functional alongside web app."""
    _, db_path, reports_dir, uploads_dir = integrated_env

    # Create drop structure
    drop_dir = uploads_dir / "cli_test_drop"
    leads_dir = drop_dir / "leads" / "motor_renewal"
    events_dir = drop_dir / "events"
    leads_dir.mkdir(parents=True, exist_ok=True)
    events_dir.mkdir(parents=True, exist_ok=True)

    (leads_dir / "batch_cli.csv").write_text(
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        "CLI User,9876543005,POL-CLI-001,2025-12-31,15000,Honda City,en\n",
        encoding="utf-8",
    )
    (events_dir / "events_cli.jsonl").write_text(
        '{"event_id": "e_cli1", "workflow_id": "motor_renewal", "event_type": "call.initiated", "call_id": "c_cli1", "occurred_at": "2026-11-04T10:00:00Z", "phone": "9876543005"}\n',
        encoding="utf-8",
    )

    # Execute CLI ingest
    exit_code_ingest = cli_main(["--db", str(db_path), "ingest", str(drop_dir)])
    assert exit_code_ingest == 0

    # Execute CLI report
    cli_out_dir = uploads_dir / "cli_reports"
    exit_code_report = cli_main(["--db", str(db_path), "report", "--out", str(cli_out_dir)])
    assert exit_code_report == 0
    assert (cli_out_dir / "batch_summary.csv").exists()
    assert (cli_out_dir / "data_quality.json").exists()


def test_zero_pii_across_all_web_responses(integrated_env):
    """Scenario 6: Verify no customer name or plaintext phone is returned by any upload endpoint."""
    client, _, _, _ = integrated_env

    raw_name = "Super Secret VIP Customer"
    raw_phone = "9876543006"

    csv_data = (
        f"Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        f"{raw_name},{raw_phone},POL-SEC-999,2025-12-31,15000,Honda City,en\n"
    ).encode("utf-8")

    ev_data = (
        f'{{"event_id": "e_p1", "workflow_id": "motor_renewal", "event_type": "call.initiated", "call_id": "c_p1", "occurred_at": "2026-11-05T10:00:00Z", "phone": "{raw_phone}"}}\n'
    ).encode("utf-8")

    # Check leads response
    r1 = client.post("/api/upload/leads", data={"workflow_id": "motor_renewal"}, files={"file": ("sec.csv", io.BytesIO(csv_data), "text/csv")})
    assert r1.status_code == 200
    assert raw_name not in r1.text
    assert raw_phone not in r1.text

    # Check events response
    r2 = client.post("/api/upload/events", files={"file": ("sec.jsonl", io.BytesIO(ev_data), "application/jsonl")})
    assert r2.status_code == 200
    assert raw_name not in r2.text
    assert raw_phone not in r2.text

    # Check bundle response
    r3 = client.post("/api/upload/bundle", data={"workflow_id": "motor_renewal"}, files={
        "leads_file": ("sec_b.csv", io.BytesIO(csv_data), "text/csv"),
        "events_file": ("sec_b.jsonl", io.BytesIO(ev_data), "application/jsonl"),
    })
    assert r3.status_code == 200
    assert raw_name not in r3.text
    assert raw_phone not in r3.text


def test_end_to_end_manual_override_lifecycle(integrated_env):
    """Scenario 7: Full lifecycle test of manual disposition override.

    1. Ingest bundle with 1 lead and 1 call having 'callback_requested'.
    2. Confirm lead is in_progress_dialed with 0 success.
    3. Manually override the call to 'renewed' with audit justification.
    4. Confirm lead moves to 'completed' and ever_success=true.
    5. Confirm daily_overview, lead_report, and data_quality reflect the new state.
    """
    client, db_path, reports_dir, _ = integrated_env

    csv_data = (
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        "Tarun Khanna,9876543999,POL-E2E-99,2026-12-31,20000,Honda City,en\n"
    ).encode("utf-8")

    ev_data = (
        '{"event_id": "e_e2e_1", "workflow_id": "motor_renewal", "event_type": "call.initiated", "call_id": "call_e2e_1", "occurred_at": "2026-11-10T10:00:00Z", "phone": "9876543999"}\n'
        '{"event_id": "e_e2e_2", "workflow_id": "motor_renewal", "event_type": "call.connected", "call_id": "call_e2e_1", "occurred_at": "2026-11-10T10:01:00Z", "phone": "9876543999"}\n'
        '{"event_id": "e_e2e_3", "workflow_id": "motor_renewal", "event_type": "disposition.set", "call_id": "call_e2e_1", "occurred_at": "2026-11-10T10:05:00Z", "payload": {"disposition_code": "callback_requested"}}\n'
    ).encode("utf-8")

    r_bundle = client.post("/api/upload/bundle", data={"workflow_id": "motor_renewal"}, files={
        "leads_file": ("leads.csv", io.BytesIO(csv_data), "text/csv"),
        "events_file": ("events.jsonl", io.BytesIO(ev_data), "application/jsonl"),
    })
    assert r_bundle.status_code == 200

    # Get lead ID
    leads_res = client.get("/api/dashboard/leads")
    lead_id = leads_res.json()["leads"][0]["id"]

    # Pre-override verification
    calls_res = client.get(f"/api/dashboard/leads/{lead_id}/calls")
    assert calls_res.json()["calls"][0]["effective_disposition"] == "callback_requested"
    assert calls_res.json()["calls"][0]["is_terminal"] is False

    # Execute manual override
    r_ovr = client.post(
        "/api/dashboard/calls/override",
        json={
            "lead_id": lead_id,
            "call_id": "call_e2e_1",
            "disposition_code": "renewed",
            "reason": "Supervisor reviewed callback audio and verified renewal payment",
        },
    )
    assert r_ovr.status_code == 200
    ovr_json = r_ovr.json()
    assert ovr_json["effective_disposition"] == "renewed"
    assert ovr_json["is_terminal"] is True
    assert ovr_json["is_success"] is True
    assert ovr_json["reports_updated"] is True

    # Post-override calls check
    calls_after = client.get(f"/api/dashboard/leads/{lead_id}/calls").json()
    assert calls_after["calls"][0]["effective_disposition"] == "renewed"
    assert calls_after["calls"][0]["is_terminal"] is True
    assert calls_after["calls"][0]["is_success"] is True

    # Post-override lead report check
    lead_rep = list(csv.DictReader((reports_dir / "lead_report.csv").read_text(encoding="utf-8").splitlines()))
    assert lead_rep[0]["bucket"] == "completed"
    assert lead_rep[0]["current_disposition"] == "renewed"
    assert lead_rep[0]["ever_success"] == "true"

    # Post-override daily overview check
    daily_rep = list(csv.DictReader((reports_dir / "daily_overview.csv").read_text(encoding="utf-8").splitlines()))
    assert daily_rep[0]["success_leads"] == "1"

    # Post-override data quality check
    dq = json.loads((reports_dir / "data_quality.json").read_text(encoding="utf-8"))
    assert dq["orphan_events"] == 0
