"""Tests for the web application structure, health endpoint, upload endpoints, and security."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from solution.web.app import create_app
from solution.web.config import (
    FileSizeLimitExceeded,
    sanitize_filename,
    save_stream_safely,
    validate_workflow_id,
)


@pytest.fixture
def app_and_dirs(tmp_path: Path):
    """Provide an isolated app with dedicated temporary database and directories."""
    db_path = tmp_path / "web_test.db"
    reports_dir = tmp_path / "reports"
    uploads_dir = tmp_path / "uploads"
    app = create_app(db_path=db_path, reports_dir=reports_dir, uploads_dir=uploads_dir)
    return app, db_path, reports_dir, uploads_dir


@pytest.fixture
def test_client(app_and_dirs):
    """Provide a TestClient for testing."""
    app, _, _, _ = app_and_dirs
    return TestClient(app)


def test_health_check_endpoint(test_client: TestClient):
    """Verify /api/health returns 200 OK and database connectivity."""
    response = test_client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["database"]["connected"] is True
    assert "motor_renewal" in data["workflows"]
    assert "policy_feedback" in data["workflows"]
    # Verify no sensitive keys leaked
    assert "secret" not in response.text.lower()
    assert "key" not in data


def test_sanitize_filename_traversal_protection():
    """Verify filename sanitization prevents path traversal attacks."""
    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename("..\\..\\windows\\system32\\cmd.exe") == "cmd.exe"
    assert sanitize_filename("safe_leads_2026.csv") == "safe_leads_2026.csv"
    assert sanitize_filename("dirty file name (1).csv") == "dirty_file_name__1_.csv"
    assert sanitize_filename(None) == "unnamed_upload"
    assert sanitize_filename("") == "unnamed_upload"


def test_workflow_validation():
    """Verify workflow validation against allowed workflows."""
    assert validate_workflow_id("motor_renewal") is True
    assert validate_workflow_id("policy_feedback") is True
    assert validate_workflow_id("unauthorized_workflow") is False
    assert validate_workflow_id("") is False


@pytest.mark.anyio
async def test_streaming_file_size_limit(tmp_path: Path):
    """Verify streaming file upload terminates immediately if size exceeds limit."""
    class MockStream:
        def __init__(self, data: bytes):
            self.stream = io.BytesIO(data)

        async def read(self, size: int) -> bytes:
            return self.stream.read(size)

    # 100 bytes allowed limit
    dest_path = tmp_path / "uploaded.dat"
    mock_small = MockStream(b"A" * 50)
    written = await save_stream_safely(mock_small, dest_path, max_bytes=100)
    assert written == 50
    assert dest_path.exists()

    # Exceed limit
    mock_large = MockStream(b"B" * 150)
    with pytest.raises(FileSizeLimitExceeded):
        await save_stream_safely(mock_large, dest_path, max_bytes=100)
    # File must be cleaned up on failure
    assert not dest_path.exists()


def test_upload_csv_motor_renewal_success(app_and_dirs):
    """Verify successful CSV upload for motor_renewal workflow."""
    app, _, reports_dir, _ = app_and_dirs
    client = TestClient(app)

    csv_content = (
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        "Rajesh Kumar,9876543210,POL-WEB-001,2025-12-31,15000,Honda City,en\n"
        "Sunita Rao,9876543211,POL-WEB-002,15/01/2026,18000,Hyundai Creta,hi\n"
    ).encode("utf-8")

    files = {"file": ("leads.csv", io.BytesIO(csv_content), "text/csv")}
    data = {"workflow_id": "motor_renewal"}

    response = client.post("/api/upload/leads", data=data, files=files)
    assert response.status_code == 200
    res = response.json()
    assert res["status"] == "success"
    assert res["workflow_id"] == "motor_renewal"
    assert res["metrics"]["lead_rows_total"] == 2
    assert res["metrics"]["lead_rows_accepted"] == 2
    assert res["metrics"]["lead_rows_rejected"] == 0
    assert res["metrics"]["duplicate_lead_rows_merged"] == 0
    assert res["reports_updated"] is True

    # Check reports were generated
    assert (reports_dir / "lead_report.csv").exists()
    assert (reports_dir / "data_quality.json").exists()


def test_upload_csv_policy_feedback_success(app_and_dirs):
    """Verify successful CSV upload for policy_feedback workflow."""
    app, _, reports_dir, _ = app_and_dirs
    client = TestClient(app)

    csv_content = (
        "Customer Name,Phone Number,Order ID,Order Date,Product,Preferred Language\n"
        "Ananya Roy,9876543212,ORD-WEB-001,2026-10-01,Comprehensive Motor,en\n"
    ).encode("utf-8")

    files = {"file": ("feedback_leads.csv", io.BytesIO(csv_content), "text/csv")}
    data = {"workflow_id": "policy_feedback"}

    response = client.post("/api/upload/leads", data=data, files=files)
    assert response.status_code == 200
    res = response.json()
    assert res["status"] == "success"
    assert res["workflow_id"] == "policy_feedback"
    assert res["metrics"]["lead_rows_total"] == 1
    assert res["metrics"]["lead_rows_accepted"] == 1


def test_upload_csv_invalid_workflow(test_client: TestClient):
    """Verify 400 Bad Request when uploading to invalid workflow."""
    csv_content = b"Customer Name,Phone Number\nAlice,9876543210\n"
    files = {"file": ("leads.csv", io.BytesIO(csv_content), "text/csv")}
    data = {"workflow_id": "unsupported_workflow_xyz"}

    response = test_client.post("/api/upload/leads", data=data, files=files)
    assert response.status_code == 400
    assert "Invalid workflow_id" in response.json()["detail"]


def test_upload_csv_invalid_extension(test_client: TestClient):
    """Verify 400 Bad Request when uploading non-csv file."""
    content = b'{"event_id": "e1"}\n'
    files = {"file": ("events.jsonl", io.BytesIO(content), "application/jsonl")}
    data = {"workflow_id": "motor_renewal"}

    response = test_client.post("/api/upload/leads", data=data, files=files)
    assert response.status_code == 400
    assert "Invalid file extension" in response.json()["detail"]


def test_upload_csv_rejections_and_zero_pii(test_client: TestClient):
    """Verify rejection details contain reasons and row numbers with ZERO plaintext PII."""
    csv_content = (
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        ",9876543220,POL-REJ-01,2025-12-31,15000,Honda City,en\n"  # Missing name
        "Secret Person,12345,POL-REJ-02,2025-12-31,15000,Honda City,en\n"  # Invalid phone
    ).encode("utf-8")

    files = {"file": ("rejected_leads.csv", io.BytesIO(csv_content), "text/csv")}
    data = {"workflow_id": "motor_renewal"}

    response = test_client.post("/api/upload/leads", data=data, files=files)
    assert response.status_code == 200
    res = response.json()
    assert res["metrics"]["lead_rows_total"] == 2
    assert res["metrics"]["lead_rows_rejected"] == 2
    assert "missing_required:customer_name" in res["rejections_by_reason"]
    assert "invalid_phone" in res["rejections_by_reason"]

    # Security check: Plaintext customer name and raw phone MUST NOT appear anywhere in the response text
    resp_text = response.text
    assert "Secret Person" not in resp_text
    assert "12345" not in resp_text
    assert "9876543220" not in resp_text


def test_upload_csv_duplicate_lead_merging(test_client: TestClient):
    """Verify duplicate lead merging across two subsequent uploads."""
    # Batch 1
    csv1 = (
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        "Vikram Singh,9876543230,POL-DUP-01,2025-12-31,15000,Honda City,en\n"
    ).encode("utf-8")
    files1 = {"file": ("batch1.csv", io.BytesIO(csv1), "text/csv")}
    r1 = test_client.post("/api/upload/leads", data={"workflow_id": "motor_renewal"}, files=files1)
    assert r1.status_code == 200
    assert r1.json()["metrics"]["duplicate_lead_rows_merged"] == 0

    # Batch 2 with same phone number
    csv2 = (
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        "Vikram Singh Updated,9876543230,POL-DUP-01B,2025-12-31,20000,Honda City,hi\n"
    ).encode("utf-8")
    files2 = {"file": ("batch2.csv", io.BytesIO(csv2), "text/csv")}
    r2 = test_client.post("/api/upload/leads", data={"workflow_id": "motor_renewal"}, files=files2)
    assert r2.status_code == 200
    assert r2.json()["metrics"]["duplicate_lead_rows_merged"] == 1


def test_upload_csv_path_traversal_filename_sanitized(test_client: TestClient):
    """Verify path traversal in filename is sanitized and processed safely."""
    csv_content = (
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        "Safe User,9876543240,POL-TRAV-01,2025-12-31,15000,Honda City,en\n"
    ).encode("utf-8")

    files = {"file": ("../../../../etc/passwd.csv", io.BytesIO(csv_content), "text/csv")}
    data = {"workflow_id": "motor_renewal"}

    response = test_client.post("/api/upload/leads", data=data, files=files)
    assert response.status_code == 200
    res = response.json()
    assert res["status"] == "success"
    # Filename must be sanitized to base name without traversal dots
    assert res["filename"] == "passwd.csv"


def test_upload_csv_idempotency(test_client: TestClient):
    """Verify that uploading the exact same file twice returns identical idempotent metrics."""
    csv_content = (
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        "Idempotent User,9876543250,POL-IDEM-01,2025-12-31,15000,Honda City,en\n"
    ).encode("utf-8")

    files1 = {"file": ("idem_batch.csv", io.BytesIO(csv_content), "text/csv")}
    r1 = test_client.post("/api/upload/leads", data={"workflow_id": "motor_renewal"}, files=files1)
    assert r1.status_code == 200

    files2 = {"file": ("idem_batch.csv", io.BytesIO(csv_content), "text/csv")}
    r2 = test_client.post("/api/upload/leads", data={"workflow_id": "motor_renewal"}, files=files2)
    assert r2.status_code == 200
    assert r1.json()["metrics"] == r2.json()["metrics"]


def test_upload_events_success(app_and_dirs):
    """Verify successful upload of valid JSONL interaction events."""
    app, db_path, reports_dir, _ = app_and_dirs
    client = TestClient(app)

    # 1. Pre-seed a matching lead
    csv_lead = (
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        "Lead For Events,9876543260,POL-EV-001,2025-12-31,15000,Honda City,en\n"
    ).encode("utf-8")
    r_lead = client.post(
        "/api/upload/leads",
        data={"workflow_id": "motor_renewal"},
        files={"file": ("lead_events.csv", io.BytesIO(csv_lead), "text/csv")},
    )
    assert r_lead.status_code == 200

    # 2. Upload events for this lead
    events_jsonl = (
        '{"event_id": "e_u1", "workflow_id": "motor_renewal", "event_type": "call.initiated", "call_id": "c_u1", "occurred_at": "2026-11-01T10:00:00Z", "phone": "9876543260"}\n'
        '{"event_id": "e_u2", "workflow_id": "motor_renewal", "event_type": "call.connected", "call_id": "c_u1", "occurred_at": "2026-11-01T10:01:00Z", "phone": "9876543260"}\n'
        '{"event_id": "e_u3", "workflow_id": "motor_renewal", "event_type": "disposition.set", "call_id": "c_u1", "occurred_at": "2026-11-01T10:05:00Z", "payload": {"disposition_code": "renewed"}}\n'
    ).encode("utf-8")

    r_ev = client.post(
        "/api/upload/events",
        files={"file": ("events.jsonl", io.BytesIO(events_jsonl), "application/jsonl")},
    )
    assert r_ev.status_code == 200
    res = r_ev.json()
    assert res["status"] == "success"
    assert res["metrics"]["events_total"] == 3
    assert res["metrics"]["events_processed"] == 3
    assert res["metrics"]["malformed_events"] == 0
    assert res["metrics"]["duplicate_events_dropped"] == 0
    assert res["metrics"]["active_orphan_events"] == 0
    assert res["reports_updated"] is True


def test_upload_events_malformed_and_duplicate(test_client: TestClient):
    """Verify malformed JSON and duplicate event IDs are counted and dropped."""
    events_jsonl = (
        '{"event_id": "dup_01", "workflow_id": "motor_renewal", "event_type": "call.initiated", "call_id": "c1", "occurred_at": "2026-11-01T10:00:00Z", "phone": "9876543261"}\n'
        '{"event_id": "dup_01", "workflow_id": "motor_renewal", "event_type": "call.connected", "call_id": "c1", "occurred_at": "2026-11-01T10:01:00Z", "phone": "9876543261"}\n'  # Duplicate ID
        'broken_json_line\n'  # Malformed line
    ).encode("utf-8")

    r = test_client.post(
        "/api/upload/events",
        files={"file": ("messy_events.jsonl", io.BytesIO(events_jsonl), "application/jsonl")},
    )
    assert r.status_code == 200
    res = r.json()
    assert res["metrics"]["events_total"] == 3
    assert res["metrics"]["events_processed"] == 1
    assert res["metrics"]["duplicate_events_dropped"] == 1
    assert res["metrics"]["malformed_events"] == 1


def test_upload_events_orphan_resolution(app_and_dirs):
    """Verify orphan events are tracked and auto-reconciled when matching lead arrives."""
    app, _, _, _ = app_and_dirs
    client = TestClient(app)

    # 1. Upload event BEFORE lead exists
    orphan_jsonl = (
        '{"event_id": "e_orph_1", "workflow_id": "motor_renewal", "event_type": "call.initiated", "call_id": "c_orph", "occurred_at": "2026-11-01T10:00:00Z", "phone": "9876543270"}\n'
    ).encode("utf-8")
    r1 = client.post(
        "/api/upload/events",
        files={"file": ("orphan.jsonl", io.BytesIO(orphan_jsonl), "application/jsonl")},
    )
    assert r1.status_code == 200
    assert r1.json()["metrics"]["active_orphan_events"] == 1

    # 2. Upload matching lead
    csv_lead = (
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        "Late Lead,9876543270,POL-LATE-01,2025-12-31,15000,Honda City,en\n"
    ).encode("utf-8")
    r2 = client.post(
        "/api/upload/leads",
        data={"workflow_id": "motor_renewal"},
        files={"file": ("late_lead.csv", io.BytesIO(csv_lead), "text/csv")},
    )
    assert r2.status_code == 200

    # 3. Upload a new dummy event to query updated orphan status
    ev_check = (
        '{"event_id": "e_check", "workflow_id": "motor_renewal", "event_type": "call.initiated", "call_id": "c_check", "occurred_at": "2026-11-01T10:00:00Z", "phone": "9876543270"}\n'
    ).encode("utf-8")
    r3 = client.post(
        "/api/upload/events",
        files={"file": ("check.jsonl", io.BytesIO(ev_check), "application/jsonl")},
    )
    assert r3.status_code == 200
    # Prior orphan has been reconciled to lead, so active orphans is 0
    assert r3.json()["metrics"]["active_orphan_events"] == 0


def test_upload_events_unknown_disposition(test_client: TestClient):
    """Verify unknown disposition code is counted and isolated."""
    events_jsonl = (
        '{"event_id": "e_unk_1", "workflow_id": "motor_renewal", "event_type": "disposition.set", "call_id": "c_unk", "occurred_at": "2026-11-01T10:00:00Z", "payload": {"disposition_code": "UNKNOWN_BOGUS_DISPOSITION"}}\n'
    ).encode("utf-8")

    r = test_client.post(
        "/api/upload/events",
        files={"file": ("unk_events.jsonl", io.BytesIO(events_jsonl), "application/jsonl")},
    )
    assert r.status_code == 200
    res = r.json()
    assert res["metrics"]["unknown_disposition_events"] == 1


def test_upload_events_invalid_extension(test_client: TestClient):
    """Verify 400 Bad Request when uploading non-jsonl file to /events."""
    content = b"Customer Name,Phone Number\nAlice,9876543210\n"
    r = test_client.post(
        "/api/upload/events",
        files={"file": ("leads.csv", io.BytesIO(content), "text/csv")},
    )
    assert r.status_code == 400
    assert "Invalid file extension" in r.json()["detail"]


def test_upload_events_filename_sanitized(test_client: TestClient):
    """Verify path traversal in event filename is sanitized."""
    events_jsonl = (
        '{"event_id": "e_trav_1", "workflow_id": "motor_renewal", "event_type": "call.initiated", "call_id": "c_tr", "occurred_at": "2026-11-01T10:00:00Z", "phone": "9876543280"}\n'
    ).encode("utf-8")

    r = test_client.post(
        "/api/upload/events",
        files={"file": ("../../../../var/log/events.jsonl", io.BytesIO(events_jsonl), "application/jsonl")},
    )
    assert r.status_code == 200
    assert r.json()["filename"] == "events.jsonl"


def test_upload_events_idempotency(test_client: TestClient):
    """Verify repeat upload of same JSONL file is idempotent."""
    events_jsonl = (
        '{"event_id": "e_idem_1", "workflow_id": "motor_renewal", "event_type": "call.initiated", "call_id": "c_idem", "occurred_at": "2026-11-01T10:00:00Z", "phone": "9876543290"}\n'
    ).encode("utf-8")

    r1 = test_client.post(
        "/api/upload/events",
        files={"file": ("idem_events.jsonl", io.BytesIO(events_jsonl), "application/jsonl")},
    )
    assert r1.status_code == 200

    r2 = test_client.post(
        "/api/upload/events",
        files={"file": ("idem_events.jsonl", io.BytesIO(events_jsonl), "application/jsonl")},
    )
    assert r2.status_code == 200
    assert r1.json()["metrics"] == r2.json()["metrics"]


def test_upload_events_zero_pii(test_client: TestClient):
    """Verify raw phone numbers from events are never returned in API response text."""
    raw_phone = "9876543295"
    events_jsonl = (
        f'{{"event_id": "e_sec_1", "workflow_id": "motor_renewal", "event_type": "call.initiated", "call_id": "c_sec", "occurred_at": "2026-11-01T10:00:00Z", "phone": "{raw_phone}"}}\n'
    ).encode("utf-8")

    r = test_client.post(
        "/api/upload/events",
        files={"file": ("sec_events.jsonl", io.BytesIO(events_jsonl), "application/jsonl")},
    )
    assert r.status_code == 200
    assert raw_phone not in r.text

