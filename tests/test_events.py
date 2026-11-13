"""Unit tests for event ingestion, deduplication, malformed handling, and unknown dispositions."""

import json
import sqlite3
import pytest
from pathlib import Path

from solution.db import get_db_connection, init_db
from solution.events import ingest_event_jsonl, parse_event_line
from solution.schemas import load_dispositions


@pytest.fixture
def memory_db():
    """Create an in-memory database initialized with schema."""
    with get_db_connection(":memory:") as conn:
        init_db(conn)
        yield conn


def test_parse_valid_event():
    """Verify parsing a standard valid event line."""
    disps = load_dispositions()
    line = json.dumps({
        "event_id": "evt_00000001",
        "event_type": "call.initiated",
        "workflow_id": "motor_renewal",
        "call_id": "call_00000001",
        "occurred_at": "2026-10-01T04:30:00Z",
        "phone": "+91 98765 43210",
        "payload": {},
    })

    status, parsed = parse_event_line(line, disps)
    assert status == "valid"
    assert parsed is not None
    assert parsed.event_id == "evt_00000001"
    assert parsed.occurred_at_utc == "2026-10-01T04:30:00Z"
    assert parsed.occurred_at_ist == "2026-10-01T10:00:00+05:30"
    assert parsed.call_date_ist == "2026-10-01"
    assert parsed.phone_masked == "+91******3210"
    assert parsed.is_unknown_disposition == 0


def test_empty_line_skipped_and_not_counted():
    """Verify empty lines are classified as 'empty'."""
    disps = load_dispositions()
    assert parse_event_line("", disps) == ("empty", None)
    assert parse_event_line("   \n", disps) == ("empty", None)


def test_malformed_json_and_missing_event_id():
    """Verify malformed JSON or missing required fields are marked 'malformed'."""
    disps = load_dispositions()

    # Broken JSON string
    assert parse_event_line('{"event_id": "evt_1", "event_type":', disps) == ("malformed", None)

    # Missing event_id
    assert parse_event_line('{"event_type": "call.initiated", "workflow_id": "motor_renewal"}', disps) == ("malformed", None)

    # Empty event_id
    assert parse_event_line('{"event_id": "  ", "event_type": "call.initiated"}', disps) == ("malformed", None)

    # Missing call_id
    assert parse_event_line('{"event_id": "evt_2", "event_type": "call.initiated", "workflow_id": "motor_renewal"}', disps) == ("malformed", None)


def test_unknown_disposition_code_counted_and_ignored():
    """Verify unknown disposition codes increment unknown_disposition_events and have None disposition_code."""
    disps = load_dispositions()

    # Valid disposition
    valid_line = json.dumps({
        "event_id": "evt_valid",
        "event_type": "disposition.set",
        "workflow_id": "motor_renewal",
        "call_id": "call_1",
        "occurred_at": "2026-10-01T10:00:00Z",
        "payload": {"disposition_code": "renewed"},
    })
    status, p_valid = parse_event_line(valid_line, disps)
    assert status == "valid"
    assert p_valid.disposition_code == "renewed"
    assert p_valid.is_unknown_disposition == 0

    # Unknown disposition code: 'RENEWD' (typo)
    bad_line = json.dumps({
        "event_id": "evt_bad",
        "event_type": "disposition.set",
        "workflow_id": "motor_renewal",
        "call_id": "call_1",
        "occurred_at": "2026-10-01T10:00:00Z",
        "payload": {"disposition_code": "RENEWD"},
    })
    status, p_bad = parse_event_line(bad_line, disps)
    assert status == "valid"
    assert p_bad.disposition_code is None  # Ignored for effective disposition
    assert p_bad.is_unknown_disposition == 1


def test_event_jsonl_ingestion_and_deduplication(tmp_path, memory_db):
    """Verify ingestion of JSONL with valid events, duplicates, malformed lines, and empty lines."""
    jsonl_file = tmp_path / "events_0001.jsonl"
    lines = [
        "",  # Empty line -> skipped
        json.dumps({
            "event_id": "evt_101",
            "event_type": "call.initiated",
            "workflow_id": "motor_renewal",
            "call_id": "call_101",
            "occurred_at": "2026-10-01 10:00:00",  # Naive (IST)
            "phone": "9876543210",
        }),
        "INVALID JSON LINE TRUNCATED",  # Malformed line
        json.dumps({
            "event_id": "evt_101",  # Duplicate event_id!
            "event_type": "call.connected",
            "workflow_id": "motor_renewal",
            "call_id": "call_101",
            "occurred_at": "2026-10-01 10:01:00",
            "phone": "9876543210",
        }),
        json.dumps({
            "event_id": "evt_102",
            "event_type": "disposition.set",
            "workflow_id": "motor_renewal",
            "call_id": "call_101",
            "occurred_at": "2026-10-01 10:05:00",
            "phone": "9876543210",
            "payload": {"disposition_code": "unknown_code"},  # Unknown disposition
        }),
    ]

    with open(jsonl_file, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    metrics = ingest_event_jsonl(jsonl_file, memory_db, batch_key_prefix="drop_1")

    assert metrics["events_total"] == 4  # 4 non-empty lines
    assert metrics["malformed_events"] == 1
    assert metrics["duplicate_events_dropped"] == 1
    assert metrics["unknown_disposition_events"] == 1

    # Check interaction_ledger table
    rows = memory_db.execute("SELECT * FROM interaction_ledger ORDER BY event_id").fetchall()
    assert len(rows) == 2  # evt_101 and evt_102 inserted
    assert rows[0]["event_id"] == "evt_101"
    # Verify append-only: duplicate evt_101 was NOT inserted and did not overwrite original
    assert rows[0]["event_type"] == "call.initiated"
    assert rows[1]["event_id"] == "evt_102"
    assert rows[1]["is_unknown_disposition"] == 1
    assert rows[1]["disposition_code"] is None


def test_event_ingestion_idempotency(tmp_path, memory_db):
    """Verify running ingest twice on the same event file changes nothing."""
    jsonl_file = tmp_path / "events_0001.jsonl"
    line = json.dumps({
        "event_id": "evt_201",
        "event_type": "call.initiated",
        "workflow_id": "motor_renewal",
        "call_id": "call_201",
        "occurred_at": "2026-10-02T05:00:00Z",
        "phone": "+91 99323 02346",
    })
    with open(jsonl_file, "w", encoding="utf-8") as f:
        f.write(line + "\n")

    m1 = ingest_event_jsonl(jsonl_file, memory_db, batch_key_prefix="drop_1")
    m2 = ingest_event_jsonl(jsonl_file, memory_db, batch_key_prefix="drop_1")

    assert m1 == m2
    count = memory_db.execute("SELECT COUNT(*) AS c FROM interaction_ledger").fetchone()["c"]
    assert count == 1
