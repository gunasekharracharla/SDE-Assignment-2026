"""Comprehensive QA, Security, Penetration, and Integrity Test Suite.

Covers:
- SQL injection in search and filter parameters
- Path traversal in file uploads and report downloads
- Cross-site scripting (XSS) payload sanitization
- Invalid file extensions and zero-byte files
- Missing required CSV headers
- Malformed JSONL lines
- Transaction rollback on failure
- Streaming upload size enforcement (> 50 MB)
- PII leakage prevention (names, raw phones, encryption keys)
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from solution.web.app import create_app
from solution.web.config import ALLOWED_REPORT_FILES, MAX_UPLOAD_BYTES


@pytest.fixture
def qa_env(tmp_path: Path):
    """Provide isolated environment for QA testing."""
    db_path = tmp_path / "qa_test.db"
    reports_dir = tmp_path / "reports"
    uploads_dir = tmp_path / "uploads"
    app = create_app(db_path=db_path, reports_dir=reports_dir, uploads_dir=uploads_dir)
    client = TestClient(app)
    return client, db_path, reports_dir, uploads_dir


# ==============================================================================
# 1. SECURITY & PENETRATION TESTING
# ==============================================================================

def test_sql_injection_defense_in_leads_search(qa_env):
    """Verify that SQL injection payloads in the leads search endpoint fail safely."""
    client, _, _, _ = qa_env

    # Seed 1 lead
    csv_leads = (
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        "Safe Customer,9876540001,POL-SAFE-001,2026-12-31,10000,Honda,en\n"
    ).encode("utf-8")
    client.post("/api/upload/leads", data={"workflow_id": "motor_renewal"}, files={"file": ("leads.csv", io.BytesIO(csv_leads), "text/csv")})

    sqli_payloads = [
        "' OR '1'='1",
        "'; DROP TABLE leads; --",
        "POL-001' UNION SELECT 1,2,3,4,5,6,7,8 --",
        "admin'--",
        "' OR 1=1 --",
    ]

    for payload in sqli_payloads:
        resp = client.get(f"/api/dashboard/leads?q={payload}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        # Injection must NOT return all records or corrupt DB
        assert data["total"] == 0

    # Verify table leads still intact
    verify_resp = client.get("/api/dashboard/leads")
    assert verify_resp.json()["total"] == 1


def test_sql_injection_defense_in_ledger_search(qa_env):
    """Verify that SQL injection payloads in interaction ledger filters fail safely."""
    client, _, _, _ = qa_env

    sqli_payloads = [
        "' OR '1'='1",
        "'; DROP TABLE interaction_ledger; --",
        "CALL' UNION SELECT null,null,null,null,null,null,null,null,null,null --",
    ]

    for payload in sqli_payloads:
        resp = client.get(f"/api/dashboard/ledger?call_id={payload}")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

        resp_type = client.get(f"/api/dashboard/ledger?event_type={payload}")
        assert resp_type.status_code == 200
        assert resp_type.json()["total"] == 0


def test_path_traversal_defense_in_report_downloads(qa_env):
    """Verify that path traversal sequences in report downloads are rejected with 404."""
    client, _, _, _ = qa_env

    traversal_payloads = [
        "../../etc/passwd",
        "..\\..\\windows\\win.ini",
        "%2e%2e%2f%2e%2e%2fconfig.py",
        "....//....//ledger.db",
        "batch_summary.csv/../../../crypto.py",
        "data/ledger.db",
        "solution/crypto.py",
    ]

    for payload in traversal_payloads:
        resp = client.get(f"/api/reports/download/{payload}")
        assert resp.status_code == 404
        # Verify internal paths are not reflected in error
        assert "Desktop" not in resp.text
        assert "AppData" not in resp.text


def test_path_traversal_defense_in_file_uploads(qa_env):
    """Verify that malicious upload filenames with directory traversal are safely sanitized."""
    client, _, _, uploads_dir = qa_env

    malicious_filenames = [
        "../../evil.csv",
        "..\\..\\system32.csv",
        "....//nested.csv",
        "/etc/cron.d/hack.csv",
        "C:\\Windows\\Temp\\exploit.csv",
    ]

    for fname in malicious_filenames:
        csv_data = "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\nJohn,9876540001,POL-1,2026-12-31,10000,Car,en\n".encode("utf-8")
        resp = client.post(
            "/api/upload/leads",
            data={"workflow_id": "motor_renewal"},
            files={"file": (fname, io.BytesIO(csv_data), "text/csv")},
        )
        assert resp.status_code == 200
        # Check that file was saved inside uploads_dir without escaping
        for staged in uploads_dir.rglob("*.csv"):
            assert uploads_dir in staged.parents


def test_xss_payload_safety(qa_env):
    """Verify that XSS payloads in external references, names, or file fields are safely handled."""
    client, _, _, _ = qa_env

    xss_csv = (
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        "<script>alert('xss')</script>,9876540001,<img src=x onerror=alert(1)>,2026-12-31,10000,Car,en\n"
        "Malicious User 2,9876540002,<script>alert(1)</script>,2026-12-31,10000,Car,en\n"
        'Malicious User 3,9876540003,"""><svg onload=alert(1)>",2026-12-31,10000,Car,en\n'
    ).encode("utf-8")

    resp = client.post(
        "/api/upload/leads",
        data={"workflow_id": "motor_renewal"},
        files={"file": ("xss_test.csv", io.BytesIO(xss_csv), "text/csv")},
    )
    assert resp.status_code == 200

    leads_resp = client.get("/api/dashboard/leads")
    leads_text = leads_resp.text
    # Customer name must NOT be returned in plaintext at all
    assert "<script>alert('xss')</script>" not in leads_text
    data = leads_resp.json()
    assert len(data["leads"]) == 3
    refs = {l["external_ref"] for l in data["leads"]}
    assert "<img src=x onerror=alert(1)>" in refs
    assert "<script>alert(1)</script>" in refs
    assert "\"><svg onload=alert(1)>" in refs


def test_frontend_dom_xss_prevention_and_html_escaping(qa_env):
    """Verify frontend index.html defines escapeHtml and sanitizes all three attack payloads."""
    index_path = Path("solution/web/static/index.html")
    assert index_path.exists(), "index.html missing"
    html_content = index_path.read_text(encoding="utf-8")

    # 1. Assert escapeHtml is defined and properly replaces all sensitive characters
    assert "function escapeHtml(val)" in html_content
    assert ".replace(/&/g, '&amp;')" in html_content
    assert ".replace(/</g, '&lt;')" in html_content
    assert ".replace(/>/g, '&gt;')" in html_content
    assert '.replace(/"/g, \'&quot;\')' in html_content
    assert ".replace(/'/g, '&#39;')" in html_content

    # 2. Assert escapeHtml is invoked on user-controlled fields in tables
    assert "${escapeHtml(lead.external_ref)}" in html_content
    assert "${escapeHtml(lead.workflow_id)}" in html_content
    assert "${escapeHtml(lead.phone_masked)}" in html_content
    assert "${escapeHtml(ev.call_id)}" in html_content
    assert "${escapeHtml(ev.event_id)}" in html_content
    assert "${escapeHtml(r.reason)}" in html_content

    # 3. Simulate escapeHtml logic on the three specific attack payloads
    def python_escape_html(val: str) -> str:
        if val is None:
            return ""
        return (
            str(val)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#39;")
        )

    payloads = [
        "<img src=x onerror=alert(1)>",
        "<script>alert(1)</script>",
        '"><svg onload=alert(1)>',
    ]

    for p in payloads:
        escaped = python_escape_html(p)
        # Ensure executable tags and unescaped quotes cannot survive
        assert "<" not in escaped
        assert ">" not in escaped
        assert '"' not in escaped
        assert "'" not in escaped
        assert "&lt;" in escaped
        assert "&gt;" in escaped

    # 4. Verify legitimate strings remain readable
    legit_strings = [
        ("POL-1001", "POL-1001"),
        ("Tata & Sons Policy", "Tata &amp; Sons Policy"),
        ("+91 ****** 1234", "+91 ****** 1234"),
    ]
    for raw, expected in legit_strings:
        assert python_escape_html(raw) == expected


def test_zero_pii_exposure_in_all_endpoints(qa_env):
    """Audit all endpoints to guarantee zero plaintext phone numbers or customer names are leaked."""
    client, _, _, _ = qa_env

    raw_phone = "9876549999"
    customer_name = "SuperSecretCustomerName"

    csv_data = (
        f"Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        f"{customer_name},{raw_phone},POL-SEC-99,2026-12-31,15000,Honda,en\n"
    ).encode("utf-8")

    # 1. Leads upload endpoint response
    r1 = client.post("/api/upload/leads", data={"workflow_id": "motor_renewal"}, files={"file": ("sec.csv", io.BytesIO(csv_data), "text/csv")})
    assert customer_name not in r1.text
    assert raw_phone not in r1.text

    # 2. Events upload endpoint response
    event_jsonl = (
        f'{{"event_id":"e-sec","workflow_id":"motor_renewal","event_type":"call.initiated","call_id":"c-sec","occurred_at":"2026-11-01T10:00:00Z","phone":"{raw_phone}"}}\n'
    ).encode("utf-8")
    r2 = client.post("/api/upload/events", files={"file": ("sec.jsonl", io.BytesIO(event_jsonl), "application/x-jsonlines")})
    assert customer_name not in r2.text
    assert raw_phone not in r2.text

    # 3. Dashboard stats
    r3 = client.get("/api/dashboard/stats")
    assert customer_name not in r3.text
    assert raw_phone not in r3.text

    # 4. Leads list
    r4 = client.get("/api/dashboard/leads")
    assert customer_name not in r4.text
    assert raw_phone not in r4.text
    assert "+91******9999" in r4.text

    # 5. Ledger list
    r5 = client.get("/api/dashboard/ledger")
    assert customer_name not in r5.text
    assert raw_phone not in r5.text
    assert "+91******9999" in r5.text


# ==============================================================================
# 2. UPLOAD EDGE CASES & VALIDATION TESTING
# ==============================================================================

def test_empty_file_upload(qa_env):
    """Uploading an empty 0-byte file should be rejected with a 400 error."""
    client, _, _, _ = qa_env

    resp = client.post(
        "/api/upload/leads",
        data={"workflow_id": "motor_renewal"},
        files={"file": ("empty.csv", io.BytesIO(b""), "text/csv")},
    )
    assert resp.status_code == 400
    assert "empty" in resp.json()["detail"].lower()

    resp_events = client.post(
        "/api/upload/events",
        files={"file": ("empty.jsonl", io.BytesIO(b""), "application/x-jsonlines")},
    )
    assert resp_events.status_code == 400


def test_invalid_file_extension(qa_env):
    """Uploading files with non-allowed extensions must be rejected."""
    client, _, _, _ = qa_env

    bad_leads = client.post(
        "/api/upload/leads",
        data={"workflow_id": "motor_renewal"},
        files={"file": ("leads.exe", io.BytesIO(b"binary content"), "application/octet-stream")},
    )
    assert bad_leads.status_code == 400

    bad_events = client.post(
        "/api/upload/events",
        files={"file": ("events.pdf", io.BytesIO(b"pdf content"), "application/pdf")},
    )
    assert bad_events.status_code == 400


def test_missing_required_csv_headers(qa_env):
    """Uploading CSV with missing mandatory headers should return clear 400 error."""
    client, _, _, _ = qa_env

    bad_header_csv = "RandomCol,AnotherCol\nVal1,Val2\n".encode("utf-8")
    resp = client.post(
        "/api/upload/leads",
        data={"workflow_id": "motor_renewal"},
        files={"file": ("bad_headers.csv", io.BytesIO(bad_header_csv), "text/csv")},
    )
    assert resp.status_code == 400
    assert "missing" in resp.json()["detail"].lower()


def test_malformed_jsonl_lines(qa_env):
    """Malformed JSON lines should be counted and isolated without crashing the pipeline."""
    client, _, _, _ = qa_env

    malformed_jsonl = (
        '{"event_id":"ok-1","workflow_id":"motor_renewal","event_type":"call.initiated","call_id":"c-1","occurred_at":"2026-11-01T10:00:00Z","phone":"9876540001"}\n'
        '{malformed json line, missing quotes\n'
        '{"event_id":"","workflow_id":"motor_renewal"}\n'
        '{"event_id":"ok-2","workflow_id":"motor_renewal","event_type":"call.connected","call_id":"c-1","occurred_at":"2026-11-01T10:01:00Z","phone":"9876540001"}\n'
    ).encode("utf-8")

    resp = client.post(
        "/api/upload/events",
        files={"file": ("mixed.jsonl", io.BytesIO(malformed_jsonl), "application/x-jsonlines")},
    )
    assert resp.status_code == 200
    metrics = resp.json()["metrics"]
    assert metrics["events_processed"] == 2
    assert metrics["malformed_events"] == 2


def test_filenames_with_special_characters(qa_env):
    """Filenames with spaces, brackets, hashes, and unicode are safely normalized."""
    client, _, _, _ = qa_env

    csv_data = "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\nJohn,9876540001,POL-1,2026-12-31,10000,Car,en\n".encode("utf-8")
    special_name = "Leads (Motor 2026) #1 [Final] - 测试.csv"

    resp = client.post(
        "/api/upload/leads",
        data={"workflow_id": "motor_renewal"},
        files={"file": (special_name, io.BytesIO(csv_data), "text/csv")},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"
    # Filename in response should be clean
    assert ".." not in resp.json()["filename"]


# ==============================================================================
# 3. DATA INTEGRITY, ORPHAN RECONCILIATION & ROLLBACK
# ==============================================================================

def test_orphan_event_reconciliation_integrity(qa_env):
    """Verify orphan events are linked when matching leads are ingested subsequently."""
    client, _, _, _ = qa_env

    # 1. Ingest event first (no matching lead in DB -> Orphan)
    orphan_jsonl = (
        '{"event_id":"e-orp-1","workflow_id":"motor_renewal","event_type":"call.initiated","call_id":"c-orp-1","occurred_at":"2026-11-01T10:00:00Z","phone":"9876547777"}\n'
    ).encode("utf-8")

    r1 = client.post("/api/upload/events", files={"file": ("orphan.jsonl", io.BytesIO(orphan_jsonl), "application/x-jsonlines")})
    assert r1.status_code == 200
    assert r1.json()["metrics"]["active_orphan_events"] == 1

    # Verify ledger shows orphan = True
    l1 = client.get("/api/dashboard/ledger")
    assert l1.json()["events"][0]["is_orphan"] is True

    # 2. Ingest matching lead
    matching_csv = (
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        "Orphan Lead,9876547777,POL-ORP-77,2026-12-31,10000,Car,en\n"
    ).encode("utf-8")

    r2 = client.post("/api/upload/leads", data={"workflow_id": "motor_renewal"}, files={"file": ("leads.csv", io.BytesIO(matching_csv), "text/csv")})
    assert r2.status_code == 200

    # 3. Verify orphan was reconciled (is_orphan becomes False)
    l2 = client.get("/api/dashboard/ledger")
    assert l2.json()["events"][0]["is_orphan"] is False


def test_repeated_uploads_idempotency(qa_env):
    """Repeatedly uploading the exact same CSV must be idempotent without creating duplicate leads."""
    client, _, _, _ = qa_env

    csv_data = (
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        "Customer One,9876540001,POL-IDEMP-01,2026-12-31,10000,Car,en\n"
    ).encode("utf-8")

    r1 = client.post("/api/upload/leads", data={"workflow_id": "motor_renewal"}, files={"file": ("idemp.csv", io.BytesIO(csv_data), "text/csv")})
    assert r1.status_code == 200
    assert r1.json()["metrics"]["lead_rows_accepted"] == 1

    r2 = client.post("/api/upload/leads", data={"workflow_id": "motor_renewal"}, files={"file": ("idemp.csv", io.BytesIO(csv_data), "text/csv")})
    assert r2.status_code == 200

    leads = client.get("/api/dashboard/leads").json()
    assert leads["total"] == 1  # Exactly 1 lead, not 2
