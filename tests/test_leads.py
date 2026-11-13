"""Unit tests for lead ingestion, validation, rejection ordering, and duplicate merging."""

import csv
import json
import sqlite3
import pytest
from pathlib import Path

from solution.crypto import decrypt_pii, hash_phone
from solution.db import get_db_connection, init_db
from solution.leads import ingest_lead_csv, validate_lead_row
from solution.schemas import load_schemas


@pytest.fixture
def memory_db():
    """Create an in-memory database initialized with schema."""
    with get_db_connection(":memory:") as conn:
        init_db(conn)
        yield conn


def test_schema_order_rejection_customer_name_before_phone():
    """Verify schema-order precedence: missing customer_name before invalid phone."""
    schemas = load_schemas()
    motor = schemas["motor_renewal"]
    header_map = motor.build_header_map()

    # Both customer_name is missing and phone is invalid (starts with 5)
    row = {
        "customer_name": "",
        "phone": "5123456789",
        "policy_number": "MOT/2026/001",
    }
    res = validate_lead_row(row, motor, header_map)
    assert res.is_rejected is True
    # In motor_renewal.json, customer_name is field 0, phone is field 1.
    assert res.rejection_reason == "missing_required:customer_name"


def test_schema_order_rejection_phone_before_policy_number():
    """Verify schema-order precedence: invalid phone before missing policy_number."""
    schemas = load_schemas()
    motor = schemas["motor_renewal"]
    header_map = motor.build_header_map()

    # customer_name is valid, phone is invalid, policy_number is missing
    row = {
        "customer_name": "Aarav Sharma",
        "phone": "5123456789",  # Invalid prefix 5
        "policy_number": "",    # Missing required
    }
    res = validate_lead_row(row, motor, header_map)
    assert res.is_rejected is True
    assert res.rejection_reason == "invalid_phone"


def test_missing_required_phone_vs_invalid_phone():
    """Verify empty phone gives missing_required:phone, while non-empty bad phone gives invalid_phone."""
    schemas = load_schemas()
    motor = schemas["motor_renewal"]
    header_map = motor.build_header_map()

    # Case 1: Empty phone
    row_empty = {
        "customer_name": "Aarav Sharma",
        "phone": "   ",
        "policy_number": "MOT/2026/001",
    }
    res_empty = validate_lead_row(row_empty, motor, header_map)
    assert res_empty.is_rejected is True
    assert res_empty.rejection_reason == "missing_required:phone"

    # Case 2: Present but invalid phone
    row_bad = {
        "customer_name": "Aarav Sharma",
        "phone": "98765",  # 5 digits
        "policy_number": "MOT/2026/001",
    }
    res_bad = validate_lead_row(row_bad, motor, header_map)
    assert res_bad.is_rejected is True
    assert res_bad.rejection_reason == "invalid_phone"


def test_blank_row_detection():
    """Verify blank rows are identified and flagged for skipping."""
    schemas = load_schemas()
    motor = schemas["motor_renewal"]
    header_map = motor.build_header_map()

    blank_row = {"customer_name": "  ", "phone": "", "policy_number": "  "}
    res = validate_lead_row(blank_row, motor, header_map)
    assert res.is_blank is True


def test_optional_invalid_fields_and_warnings():
    """Verify invalid optional fields become None and increment warnings on accepted rows."""
    schemas = load_schemas()
    motor = schemas["motor_renewal"]
    header_map = motor.build_header_map()

    row = {
        "customer_name": "Rohan Gupta",
        "phone": "+91 98765 43210",
        "policy_number": "MOT/2026/002",
        "policy_expiry": "31/02/2026",       # Invalid date
        "premium_amount": "twelve thousand", # Invalid decimal
        "preferred_language": "Hindi",       # Invalid enum
    }
    res = validate_lead_row(row, motor, header_map)
    assert res.is_rejected is False
    assert res.field_warnings == 3
    assert res.extra_data.get("policy_expiry") is None
    assert res.extra_data.get("premium_amount") is None
    assert res.preferred_language is None


def test_optional_invalid_fields_on_rejected_row_gives_no_warnings():
    """Verify warnings are NOT counted when a row is rejected."""
    schemas = load_schemas()
    motor = schemas["motor_renewal"]
    header_map = motor.build_header_map()

    row = {
        "customer_name": "",                 # Missing required -> Rejected!
        "phone": "+91 98765 43210",
        "policy_number": "MOT/2026/003",
        "policy_expiry": "31/02/2026",       # Invalid date
    }
    res = validate_lead_row(row, motor, header_map)
    assert res.is_rejected is True
    assert res.rejection_reason == "missing_required:customer_name"
    assert res.field_warnings == 0  # Warnings only count for accepted rows


def test_lead_csv_ingestion_and_pii_protection(tmp_path, memory_db):
    """Verify CSV ingestion, rejection logging without PII, and PII encryption at rest."""
    csv_file = tmp_path / "batch_001.csv"
    with open(csv_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["customer_name", "phone", "policy_number", "policy_expiry", "premium_amount", "preferred_language"])
        writer.writerow(["", "", "", "", "", ""])  # Row 1: Blank row -> skipped
        writer.writerow(["Aarav Sharma", "+91 98765 43210", "MOT/2026/101", "2026-10-31", "₹15,000", "en"])  # Row 1 non-blank -> Valid
        writer.writerow(["", "9876543211", "MOT/2026/102", "2026-10-31", "10000", "hi"])  # Row 2 non-blank -> Rejected (no name)
        writer.writerow(["Kavya Patel", "5123456789", "MOT/2026/103", "", "", ""])  # Row 3 non-blank -> Rejected (invalid phone)

    metrics = ingest_lead_csv(csv_file, "motor_renewal", memory_db, batch_key_prefix="drop_1")
    assert metrics["lead_rows_total"] == 3  # Blank row was not counted!
    assert metrics["lead_rows_rejected"] == 2
    assert metrics["duplicate_lead_rows_merged"] == 0
    assert metrics["field_warnings"] == 0

    # Verify leads table
    lead = memory_db.execute("SELECT * FROM leads WHERE workflow_id = 'motor_renewal'").fetchone()
    assert lead is not None
    assert lead["batch_id"] == "batch_001"
    assert lead["phone_masked"] == "+91******3210"
    # Decrypt and check plaintext
    assert decrypt_pii(lead["customer_name_encrypted"]) == "Aarav Sharma"
    assert decrypt_pii(lead["phone_encrypted"]) == "+919876543210"

    # Verify rejections table contains NO PII
    rejections = memory_db.execute(
        "SELECT row_number, reason FROM rejections WHERE workflow_id = 'motor_renewal' ORDER BY row_number"
    ).fetchall()
    assert len(rejections) == 2
    assert rejections[0]["row_number"] == 2
    assert rejections[0]["reason"] == "missing_required:customer_name"
    assert rejections[1]["row_number"] == 3
    assert rejections[1]["reason"] == "invalid_phone"


def test_duplicate_lead_merging_and_permanent_batch_id(tmp_path, memory_db):
    """Verify duplicate leads preserve original batch_id and overwrite non-null fields."""
    # Batch 001
    b1_file = tmp_path / "batch_001.csv"
    with open(b1_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["customer_name", "phone", "policy_number", "premium_amount", "preferred_language"])
        writer.writerow(["Priya Sharma", "9876543210", "MOT/2026/201", "5000", "en"])
    ingest_lead_csv(b1_file, "motor_renewal", memory_db, batch_key_prefix="drop_1")

    # Batch 002 (Duplicate with updated amount and name)
    b2_file = tmp_path / "batch_002.csv"
    with open(b2_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["customer_name", "phone", "policy_number", "premium_amount", "preferred_language"])
        writer.writerow(["Priya S.", "+91 98765 43210", "MOT/2026/201", "7500", ""])
    m2 = ingest_lead_csv(b2_file, "motor_renewal", memory_db, batch_key_prefix="drop_1")

    assert m2["duplicate_lead_rows_merged"] == 1

    # Check lead record in database
    lead = memory_db.execute("SELECT * FROM leads WHERE workflow_id = 'motor_renewal'").fetchone()
    # Batch ID must remain batch_001 permanently
    assert lead["batch_id"] == "batch_001"
    # Customer name updated to 'Priya S.'
    assert decrypt_pii(lead["customer_name_encrypted"]) == "Priya S."
    # Language remained 'en' (because batch_002 had empty language)
    assert lead["preferred_language"] == "en"
    # Extra data has updated premium_amount = 7500.0
    extra = json.loads(lead["extra_data"])
    assert extra["premium_amount"] == 7500.0


def test_ingestion_idempotency(tmp_path, memory_db):
    """Verify running ingest twice on the same CSV changes nothing."""
    csv_file = tmp_path / "batch_001.csv"
    with open(csv_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["customer_name", "phone", "policy_number", "premium_amount"])
        writer.writerow(["Nikhil Nair", "9932302346", "MOT/2026/301", "6850"])

    m1 = ingest_lead_csv(csv_file, "motor_renewal", memory_db, batch_key_prefix="drop_1")
    # Re-run same batch
    m2 = ingest_lead_csv(csv_file, "motor_renewal", memory_db, batch_key_prefix="drop_1")

    assert m1 == m2
    total_leads = memory_db.execute("SELECT COUNT(*) AS c FROM leads").fetchone()["c"]
    assert total_leads == 1
