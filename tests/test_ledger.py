"""Unit tests for event-to-lead linking, orphan resolution, effective dispositions, overrides, and call bucketing."""

import csv
import json
import sqlite3
import pytest
from pathlib import Path

from solution.db import get_db_connection, init_db
from solution.events import ingest_event_jsonl
from solution.leads import ingest_lead_csv
from solution.ledger import (
    CallSummary,
    determine_lead_bucket,
    get_call_summaries,
    get_lead_metrics,
    reconcile_orphan_events,
)


@pytest.fixture
def memory_db():
    """Create an in-memory database initialized with schema."""
    with get_db_connection(":memory:") as conn:
        init_db(conn)
        yield conn


def test_events_before_leads_orphan_resolution(tmp_path, memory_db):
    """Verify events arriving in drop_1 before lead arrives in drop_2 are initially orphaned and later resolved."""
    phone = "+91 98765 43210"

    # Step 1: Ingest event for phone not yet in leads
    jsonl_file = tmp_path / "events_0001.jsonl"
    with open(jsonl_file, "w", encoding="utf-8") as f:
        f.write(json.dumps({
            "event_id": "evt_orph_1",
            "event_type": "call.initiated",
            "workflow_id": "motor_renewal",
            "call_id": "call_orph_1",
            "occurred_at": "2026-10-01T04:30:00Z",
            "phone": phone,
        }) + "\n")

    ingest_event_jsonl(jsonl_file, memory_db, batch_key_prefix="drop_1")

    # Event must be orphan initially
    evt = memory_db.execute("SELECT is_orphan, lead_id FROM interaction_ledger WHERE event_id = 'evt_orph_1'").fetchone()
    assert evt["is_orphan"] == 1
    assert evt["lead_id"] is None

    # Step 2: Now ingest the matching lead
    csv_file = tmp_path / "batch_001.csv"
    with open(csv_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["customer_name", "phone", "policy_number"])
        writer.writerow(["Aarav Sharma", phone, "MOT/2026/999"])

    ingest_lead_csv(csv_file, "motor_renewal", memory_db, batch_key_prefix="drop_2")

    # Step 3: Event must now be linked to the newly created lead!
    evt_after = memory_db.execute("SELECT is_orphan, lead_id FROM interaction_ledger WHERE event_id = 'evt_orph_1'").fetchone()
    lead = memory_db.execute("SELECT id FROM leads WHERE workflow_id = 'motor_renewal'").fetchone()
    assert evt_after["is_orphan"] == 0
    assert evt_after["lead_id"] == lead["id"]


def test_leads_before_events_linking(tmp_path, memory_db):
    """Verify events arriving after lead is uploaded are linked immediately."""
    phone = "+91 91234 56789"

    # Ingest lead first
    csv_file = tmp_path / "batch_001.csv"
    with open(csv_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["customer_name", "phone", "policy_number"])
        writer.writerow(["Diya Nair", phone, "MOT/2026/888"])
    ingest_lead_csv(csv_file, "motor_renewal", memory_db, batch_key_prefix="drop_1")

    # Ingest event
    jsonl_file = tmp_path / "events_0001.jsonl"
    with open(jsonl_file, "w", encoding="utf-8") as f:
        f.write(json.dumps({
            "event_id": "evt_lead_first",
            "event_type": "call.initiated",
            "workflow_id": "motor_renewal",
            "call_id": "call_lead_first",
            "occurred_at": "2026-10-01T04:30:00Z",
            "phone": phone,
        }) + "\n")
    ingest_event_jsonl(jsonl_file, memory_db, batch_key_prefix="drop_1")

    evt = memory_db.execute("SELECT is_orphan, lead_id FROM interaction_ledger WHERE event_id = 'evt_lead_first'").fetchone()
    lead = memory_db.execute("SELECT id FROM leads WHERE workflow_id = 'motor_renewal'").fetchone()
    assert evt["is_orphan"] == 0
    assert evt["lead_id"] == lead["id"]


def test_override_before_call_event_resolution(tmp_path, memory_db):
    """Verify disposition.override arriving before call.initiated resolves via call_id."""
    phone = "+91 97777 88888"

    # Create lead
    csv_file = tmp_path / "batch_001.csv"
    with open(csv_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["customer_name", "phone", "policy_number"])
        writer.writerow(["Nikhil K", phone, "MOT/2026/777"])
    ingest_lead_csv(csv_file, "motor_renewal", memory_db, batch_key_prefix="drop_1")

    # Ingest disposition.override FIRST (has no phone, only call_id)
    jsonl_file = tmp_path / "events_0001.jsonl"
    with open(jsonl_file, "w", encoding="utf-8") as f:
        # Override appears first in file
        f.write(json.dumps({
            "event_id": "evt_over_1",
            "event_type": "disposition.override",
            "workflow_id": "motor_renewal",
            "call_id": "call_ooo_1",
            "occurred_at": "2026-10-01T06:00:00Z",
            "payload": {"disposition_code": "renewed", "overridden_by": "sup_1"},
        }) + "\n")
        # Then call.initiated appears
        f.write(json.dumps({
            "event_id": "evt_init_1",
            "event_type": "call.initiated",
            "workflow_id": "motor_renewal",
            "call_id": "call_ooo_1",
            "occurred_at": "2026-10-01T05:00:00Z",
            "phone": phone,
        }) + "\n")

    ingest_event_jsonl(jsonl_file, memory_db, batch_key_prefix="drop_1")

    # Both events must be linked to the lead
    lead = memory_db.execute("SELECT id FROM leads WHERE workflow_id = 'motor_renewal'").fetchone()
    override_evt = memory_db.execute("SELECT is_orphan, lead_id FROM interaction_ledger WHERE event_id = 'evt_over_1'").fetchone()
    init_evt = memory_db.execute("SELECT is_orphan, lead_id FROM interaction_ledger WHERE event_id = 'evt_init_1'").fetchone()

    assert init_evt["is_orphan"] == 0
    assert init_evt["lead_id"] == lead["id"]
    assert override_evt["is_orphan"] == 0
    assert override_evt["lead_id"] == lead["id"]


def test_effective_disposition_precedence_and_multiple_overrides(tmp_path, memory_db):
    """Verify override takes precedence over set, and latest override by occurred_at wins."""
    phone = "+91 95555 44444"

    # Create lead
    csv_file = tmp_path / "batch_001.csv"
    with open(csv_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["customer_name", "phone", "policy_number"])
        writer.writerow(["Kavya M", phone, "MOT/2026/555"])
    ingest_lead_csv(csv_file, "motor_renewal", memory_db, batch_key_prefix="drop_1")

    # Events with out-of-order arrival:
    # 1. call.initiated at 10:00
    # 2. disposition.set = 'callback_requested' at 10:15
    # 3. disposition.override = 'wrong_number' at 12:00
    # 4. disposition.override = 'renewed' at 14:00 (Latest override -> MUST WIN)
    events = [
        {"event_id": "e1", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "c1", "occurred_at": "2026-10-01T04:30:00Z", "phone": phone},
        {"event_id": "e2", "event_type": "call.connected", "workflow_id": "motor_renewal", "call_id": "c1", "occurred_at": "2026-10-01T04:31:00Z", "phone": phone},
        # Intentionally write the 14:00 override BEFORE the 12:00 override to test timestamp ordering
        {"event_id": "e4", "event_type": "disposition.override", "workflow_id": "motor_renewal", "call_id": "c1", "occurred_at": "2026-10-01T08:30:00Z", "payload": {"disposition_code": "renewed"}},
        {"event_id": "e3", "event_type": "disposition.override", "workflow_id": "motor_renewal", "call_id": "c1", "occurred_at": "2026-10-01T06:30:00Z", "payload": {"disposition_code": "wrong_number"}},
        {"event_id": "e5", "event_type": "disposition.set", "workflow_id": "motor_renewal", "call_id": "c1", "occurred_at": "2026-10-01T04:45:00Z", "phone": phone, "payload": {"disposition_code": "callback_requested"}},
    ]
    jsonl_file = tmp_path / "events_0001.jsonl"
    with open(jsonl_file, "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")
    ingest_event_jsonl(jsonl_file, memory_db, batch_key_prefix="drop_1")

    summaries = get_call_summaries(memory_db)
    assert "c1" in summaries
    c1 = summaries["c1"]
    assert c1.is_connected is True
    # The 14:00 UTC override 'renewed' must win!
    assert c1.effective_disposition == "renewed"
    assert c1.is_terminal is True
    assert c1.is_success is True


def test_calls_without_call_initiated_not_counted(tmp_path, memory_db):
    """Verify interactions missing call.initiated are not counted as valid calls."""
    phone = "+91 94444 33333"

    csv_file = tmp_path / "batch_001.csv"
    with open(csv_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["customer_name", "phone", "policy_number"])
        writer.writerow(["Arjun V", phone, "MOT/2026/444"])
    ingest_lead_csv(csv_file, "motor_renewal", memory_db, batch_key_prefix="drop_1")

    # Has call.connected and call.ended, but NO call.initiated!
    events = [
        {"event_id": "e_no_init_1", "event_type": "call.connected", "workflow_id": "motor_renewal", "call_id": "call_bad", "occurred_at": "2026-10-01T04:30:00Z", "phone": phone},
        {"event_id": "e_no_init_2", "event_type": "call.ended", "workflow_id": "motor_renewal", "call_id": "call_bad", "occurred_at": "2026-10-01T04:35:00Z", "phone": phone},
    ]
    jsonl_file = tmp_path / "events_0001.jsonl"
    with open(jsonl_file, "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")
    ingest_event_jsonl(jsonl_file, memory_db, batch_key_prefix="drop_1")

    summaries = get_call_summaries(memory_db)
    assert "call_bad" not in summaries  # Missing call.initiated -> NOT a counted call

    lead_metrics = get_lead_metrics(memory_db)
    lead = memory_db.execute("SELECT id FROM leads WHERE workflow_id = 'motor_renewal'").fetchone()
    lm = lead_metrics[lead["id"]]
    assert lm.total_calls == 0
    assert lm.bucket == "yet_to_reach_out"


def test_call_buckets_and_precedence():
    """Verify all four bucket rules and precedence in determine_lead_bucket."""
    # 1. No calls -> yet_to_reach_out
    assert determine_lead_bucket([]) == "yet_to_reach_out"

    # Dummy call generator
    def make_call(call_id: str, is_terminal: bool, has_telephony_failed: bool, is_connected: bool = True):
        return CallSummary(
            call_id=call_id,
            workflow_id="motor_renewal",
            lead_id=1,
            initiated_at_utc="2026-10-01T04:30:00Z",
            initiated_at_ist="2026-10-01T10:00:00+05:30",
            call_date_ist="2026-10-01",
            is_connected=is_connected,
            has_telephony_failed=has_telephony_failed,
            effective_disposition="code",
            is_terminal=is_terminal,
            is_success=False,
        )

    # 2. Any call terminal -> completed (takes precedence over telephony_failed)
    c_term = make_call("c1", is_terminal=True, has_telephony_failed=False)
    c_tel_fail = make_call("c2", is_terminal=False, has_telephony_failed=True, is_connected=False)
    assert determine_lead_bucket([c_term]) == "completed"
    # Even if another call has telephony failed, terminal call makes bucket completed!
    assert determine_lead_bucket([c_tel_fail, c_term]) == "completed"

    # 3. Every call has telephony_failed -> in_progress_telephony_failed
    c_tel_fail2 = make_call("c3", is_terminal=False, has_telephony_failed=True, is_connected=False)
    assert determine_lead_bucket([c_tel_fail, c_tel_fail2]) == "in_progress_telephony_failed"

    # 4. Dialed but not all telephony failed and none terminal -> in_progress_dialed
    c_dialed = make_call("c4", is_terminal=False, has_telephony_failed=False, is_connected=True)
    assert determine_lead_bucket([c_dialed]) == "in_progress_dialed"
    # Mixed dialed and telephony failed -> in_progress_dialed
    assert determine_lead_bucket([c_tel_fail, c_dialed]) == "in_progress_dialed"


def test_multiple_calls_for_one_lead_metrics(tmp_path, memory_db):
    """Verify total_calls, connected_calls, last_call_at, current_disposition, and ever_success."""
    phone = "+91 93333 22222"

    csv_file = tmp_path / "batch_001.csv"
    with open(csv_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["customer_name", "phone", "policy_number"])
        writer.writerow(["Varun S", phone, "MOT/2026/333"])
    ingest_lead_csv(csv_file, "motor_renewal", memory_db, batch_key_prefix="drop_1")

    # Lead gets 3 calls:
    # Call 1 at 10:00: connected, callback_requested (non-terminal, non-success)
    # Call 2 at 12:00: telephony_failed (not connected, no disposition)
    # Call 3 at 14:00: connected, renewed (terminal, success)
    events = [
        # Call 1
        {"event_id": "c1_init", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "call_1", "occurred_at": "2026-10-01T04:30:00Z", "phone": phone},
        {"event_id": "c1_conn", "event_type": "call.connected", "workflow_id": "motor_renewal", "call_id": "call_1", "occurred_at": "2026-10-01T04:31:00Z", "phone": phone},
        {"event_id": "c1_disp", "event_type": "disposition.set", "workflow_id": "motor_renewal", "call_id": "call_1", "occurred_at": "2026-10-01T04:35:00Z", "phone": phone, "payload": {"disposition_code": "callback_requested"}},
        # Call 2
        {"event_id": "c2_init", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "call_2", "occurred_at": "2026-10-01T06:30:00Z", "phone": phone},
        {"event_id": "c2_fail", "event_type": "call.telephony_failed", "workflow_id": "motor_renewal", "call_id": "call_2", "occurred_at": "2026-10-01T06:31:00Z", "phone": phone, "payload": {"reason": "sip_503"}},
        # Call 3
        {"event_id": "c3_init", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "call_3", "occurred_at": "2026-10-01T08:30:00Z", "phone": phone},
        {"event_id": "c3_conn", "event_type": "call.connected", "workflow_id": "motor_renewal", "call_id": "call_3", "occurred_at": "2026-10-01T08:31:00Z", "phone": phone},
        {"event_id": "c3_disp", "event_type": "disposition.set", "workflow_id": "motor_renewal", "call_id": "call_3", "occurred_at": "2026-10-01T08:35:00Z", "phone": phone, "payload": {"disposition_code": "renewed"}},
    ]
    jsonl_file = tmp_path / "events_0001.jsonl"
    with open(jsonl_file, "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")
    ingest_event_jsonl(jsonl_file, memory_db, batch_key_prefix="drop_1")

    lead_metrics = get_lead_metrics(memory_db)
    lead = memory_db.execute("SELECT id FROM leads WHERE workflow_id = 'motor_renewal'").fetchone()
    lm = lead_metrics[lead["id"]]

    assert lm.total_calls == 3
    assert lm.connected_calls == 2
    # Last call at IST is Call 3: 08:30 UTC = 14:00 IST
    assert lm.last_call_at_ist == "2026-10-01T14:00:00+05:30"
    # Current disposition is Call 3: 'renewed'
    assert lm.current_disposition == "renewed"
    assert lm.ever_success is True
    assert lm.bucket == "completed"
