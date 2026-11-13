"""Interaction ledger query engine, effective disposition resolution, and call bucketing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import sqlite3

from solution.schemas import DispositionInfo, load_dispositions


@dataclass
class CallSummary:
    call_id: str
    workflow_id: str
    lead_id: int
    initiated_at_utc: str
    initiated_at_ist: str
    call_date_ist: str
    is_connected: bool
    has_telephony_failed: bool
    effective_disposition: Optional[str]
    is_terminal: bool
    is_success: bool


@dataclass
class LeadMetrics:
    lead_id: int
    workflow_id: str
    total_calls: int
    connected_calls: int
    last_call_at_ist: Optional[str]
    current_disposition: Optional[str]
    ever_success: bool
    bucket: str


def reconcile_orphan_events(conn: sqlite3.Connection) -> int:
    """Reconcile unlinked orphan events with leads and calls.
    
    1. Link events having phone_hash to matching leads.
    2. Link overrides and events without phone to lead via other events of the same call_id.
    
    Returns total count of events resolved from orphan to linked.
    """
    # 1. Link orphan events that have phone_hash to leads
    cursor1 = conn.execute(
        """
        UPDATE interaction_ledger
        SET lead_id = (
            SELECT leads.id FROM leads
            WHERE leads.workflow_id = interaction_ledger.workflow_id
              AND leads.phone_hash = interaction_ledger.phone_hash
        ),
        is_orphan = 0
        WHERE is_orphan = 1
          AND phone_hash IS NOT NULL
          AND EXISTS (
            SELECT 1 FROM leads
            WHERE leads.workflow_id = interaction_ledger.workflow_id
              AND leads.phone_hash = interaction_ledger.phone_hash
          )
        """
    )
    resolved_by_phone = cursor1.rowcount

    # 2. Link orphan events (like disposition.override) via call_id to already-linked call events
    cursor2 = conn.execute(
        """
        UPDATE interaction_ledger
        SET lead_id = (
            SELECT parent.lead_id FROM interaction_ledger parent
            WHERE parent.call_id = interaction_ledger.call_id
              AND parent.lead_id IS NOT NULL
            LIMIT 1
        ),
        phone_hash = COALESCE(
            interaction_ledger.phone_hash,
            (SELECT parent.phone_hash FROM interaction_ledger parent
             WHERE parent.call_id = interaction_ledger.call_id
               AND parent.phone_hash IS NOT NULL
             LIMIT 1)
        ),
        phone_masked = COALESCE(
            interaction_ledger.phone_masked,
            (SELECT parent.phone_masked FROM interaction_ledger parent
             WHERE parent.call_id = interaction_ledger.call_id
               AND parent.phone_masked IS NOT NULL
             LIMIT 1)
        ),
        is_orphan = 0
        WHERE is_orphan = 1
          AND EXISTS (
            SELECT 1 FROM interaction_ledger parent
            WHERE parent.call_id = interaction_ledger.call_id
              AND parent.lead_id IS NOT NULL
          )
        """
    )
    resolved_by_call = cursor2.rowcount

    return resolved_by_phone + resolved_by_call


def determine_lead_bucket(calls: List[CallSummary]) -> str:
    """Determine lead bucket per specification precedence:
    
    1. yet_to_reach_out: no calls.
    2. completed: any call's effective disposition is terminal (is_terminal=true).
    3. in_progress_telephony_failed: every call has a call.telephony_failed event.
    4. in_progress_dialed: everything else.
    """
    # 1. No calls -> yet_to_reach_out
    if not calls:
        return "yet_to_reach_out"

    # 2. Any call's effective disposition is terminal -> completed
    if any(c.is_terminal for c in calls):
        return "completed"

    # 3. Every call has telephony_failed -> in_progress_telephony_failed
    if all(c.has_telephony_failed for c in calls):
        return "in_progress_telephony_failed"

    # 4. Everything else -> in_progress_dialed
    return "in_progress_dialed"


def get_call_summaries(conn: sqlite3.Connection) -> Dict[str, CallSummary]:
    """Retrieve all valid calls with their effective dispositions, connectedness, and terminal/success flags.
    
    Rule: A call is counted only if its call.initiated event is present and linked.
    """
    dispositions = load_dispositions()

    # 1. Find all valid initiated calls
    # If multiple call.initiated exist for the same call, order by occurred_at_utc ASC to get earliest
    initiated_query = """
    WITH ranked_initiated AS (
        SELECT 
            call_id,
            workflow_id,
            lead_id,
            occurred_at_utc,
            occurred_at_ist,
            call_date_ist,
            ROW_NUMBER() OVER (PARTITION BY call_id ORDER BY occurred_at_utc ASC) AS rnk
        FROM interaction_ledger
        WHERE lead_id IS NOT NULL 
          AND is_orphan = 0
          AND event_type = 'call.initiated'
    )
    SELECT call_id, workflow_id, lead_id, occurred_at_utc, occurred_at_ist, call_date_ist
    FROM ranked_initiated
    WHERE rnk = 1;
    """
    initiated_rows = conn.execute(initiated_query).fetchall()
    if not initiated_rows:
        return {}

    valid_call_ids = [r["call_id"] for r in initiated_rows]
    valid_call_set = set(valid_call_ids)

    # 2. Check connected calls (call.connected event exists and is linked)
    connected_calls_set: set[str] = {
        r["call_id"]
        for r in conn.execute(
            """
            SELECT DISTINCT call_id FROM interaction_ledger
            WHERE lead_id IS NOT NULL AND is_orphan = 0 AND event_type = 'call.connected'
            """
        ).fetchall()
        if r["call_id"] in valid_call_set
    }

    # 3. Check telephony failed calls (call.telephony_failed event exists and is linked)
    telephony_failed_set: set[str] = {
        r["call_id"]
        for r in conn.execute(
            """
            SELECT DISTINCT call_id FROM interaction_ledger
            WHERE lead_id IS NOT NULL AND is_orphan = 0 AND event_type = 'call.telephony_failed'
            """
        ).fetchall()
        if r["call_id"] in valid_call_set
    }

    # 4. Determine effective disposition for each call
    # Rule: Latest disposition.override by occurred_at. If no override, latest disposition.set.
    # Note: unknown disposition codes are already filtered (is_unknown_disposition = 0, disposition_code IS NOT NULL)
    effective_disp_query = """
    WITH ranked_disps AS (
        SELECT 
            call_id,
            disposition_code,
            event_type,
            occurred_at_utc,
            ROW_NUMBER() OVER (
                PARTITION BY call_id
                ORDER BY 
                    CASE WHEN event_type = 'disposition.override' THEN 1 ELSE 2 END ASC,
                    occurred_at_utc DESC
            ) AS rnk
        FROM interaction_ledger
        WHERE lead_id IS NOT NULL 
          AND is_orphan = 0
          AND event_type IN ('disposition.set', 'disposition.override')
          AND disposition_code IS NOT NULL
          AND is_unknown_disposition = 0
    )
    SELECT call_id, disposition_code
    FROM ranked_disps
    WHERE rnk = 1;
    """
    effective_disps: Dict[str, str] = {
        r["call_id"]: r["disposition_code"]
        for r in conn.execute(effective_disp_query).fetchall()
    }

    summaries: Dict[str, CallSummary] = {}
    for r in initiated_rows:
        cid = r["call_id"]
        wf_id = r["workflow_id"]
        lead_id = r["lead_id"]
        eff_code = effective_disps.get(cid)

        is_term = False
        is_succ = False
        if eff_code:
            wf_disps = dispositions.get(wf_id, {})
            disp_info = wf_disps.get(eff_code)
            if disp_info:
                is_term = disp_info.is_terminal
                is_succ = disp_info.is_success

        summaries[cid] = CallSummary(
            call_id=cid,
            workflow_id=wf_id,
            lead_id=lead_id,
            initiated_at_utc=r["occurred_at_utc"],
            initiated_at_ist=r["occurred_at_ist"],
            call_date_ist=r["call_date_ist"],
            is_connected=(cid in connected_calls_set),
            has_telephony_failed=(cid in telephony_failed_set),
            effective_disposition=eff_code,
            is_terminal=is_term,
            is_success=is_succ,
        )

    return summaries


def get_lead_metrics(conn: sqlite3.Connection) -> Dict[int, LeadMetrics]:
    """Calculate derived metrics and call bucket for every lead in the database."""
    call_summaries = get_call_summaries(conn)

    # Group calls by lead_id
    calls_by_lead: Dict[int, List[CallSummary]] = {}
    for call in call_summaries.values():
        calls_by_lead.setdefault(call.lead_id, []).append(call)

    # Sort each lead's calls by initiated_at_utc ASC
    for lid in calls_by_lead:
        calls_by_lead[lid].sort(key=lambda c: c.initiated_at_utc)

    all_leads = conn.execute("SELECT id, workflow_id FROM leads").fetchall()
    metrics: Dict[int, LeadMetrics] = {}

    for lead_row in all_leads:
        lead_id = lead_row["id"]
        workflow_id = lead_row["workflow_id"]
        calls = calls_by_lead.get(lead_id, [])

        total_calls = len(calls)
        connected_calls = sum(1 for c in calls if c.is_connected)

        # last_call_at: the latest call.initiated time as ISO-8601 in IST, or None
        last_call_at_ist = calls[-1].initiated_at_ist if calls else None

        # current_disposition: effective disposition of most recent call (by initiated time) that has one
        current_disp: Optional[str] = None
        for c in reversed(calls):
            if c.effective_disposition:
                current_disp = c.effective_disposition
                break

        # ever_success: whether any call's effective disposition has is_success=true
        ever_success = any(c.is_success for c in calls)

        # bucket calculation
        bucket = determine_lead_bucket(calls)

        metrics[lead_id] = LeadMetrics(
            lead_id=lead_id,
            workflow_id=workflow_id,
            total_calls=total_calls,
            connected_calls=connected_calls,
            last_call_at_ist=last_call_at_ist,
            current_disposition=current_disp,
            ever_success=ever_success,
            bucket=bucket,
        )

    return metrics
