"""Event JSON Lines ingestion, validation, deduplication, and ledger storage."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import sqlite3

from solution.config import compute_file_checksum
from solution.crypto import hash_phone, mask_phone
from solution.ledger import reconcile_orphan_events
from solution.normalizers import normalize_phone, parse_timestamp
from solution.schemas import DispositionInfo, load_dispositions


@dataclass
class ParsedEvent:
    event_id: str
    workflow_id: str
    event_type: str
    call_id: str
    occurred_at_utc: str
    occurred_at_ist: str
    call_date_ist: str
    phone_hash: Optional[str]
    phone_masked: Optional[str]
    payload_json: str
    disposition_code: Optional[str]
    is_unknown_disposition: int


def parse_event_line(
    line: str,
    dispositions: Dict[str, Dict[str, DispositionInfo]],
) -> Tuple[str, Optional[ParsedEvent]]:
    """Parse and validate a single event JSON line.
    
    Returns:
        (status, parsed_event_or_none)
        status is one of: 'empty', 'malformed', 'valid'
    """
    raw_stripped = line.strip()
    if not raw_stripped:
        return "empty", None

    try:
        data = json.loads(raw_stripped)
    except Exception:
        return "malformed", None

    if not isinstance(data, dict):
        return "malformed", None

    event_id = data.get("event_id")
    workflow_id = data.get("workflow_id")
    event_type = data.get("event_type")
    call_id = data.get("call_id")
    occurred_at_raw = data.get("occurred_at")

    # Essential fields must be non-empty strings
    if not (event_id and isinstance(event_id, str) and event_id.strip()):
        return "malformed", None
    if not (workflow_id and isinstance(workflow_id, str) and workflow_id.strip()):
        return "malformed", None
    if not (event_type and isinstance(event_type, str) and event_type.strip()):
        return "malformed", None
    if not (call_id and isinstance(call_id, str) and call_id.strip()):
        return "malformed", None
    if not (occurred_at_raw and isinstance(occurred_at_raw, str) and occurred_at_raw.strip()):
        return "malformed", None

    event_id = event_id.strip()
    workflow_id = workflow_id.strip()
    event_type = event_type.strip()
    call_id = call_id.strip()

    try:
        dt_utc, dt_ist, utc_iso, ist_iso, ist_date = parse_timestamp(occurred_at_raw)
    except Exception:
        return "malformed", None

    # Handle phone (if present)
    raw_phone = data.get("phone")
    phone_h: Optional[str] = None
    phone_m: Optional[str] = None
    if raw_phone and isinstance(raw_phone, str) and raw_phone.strip():
        ok, canonical_p = normalize_phone(raw_phone.strip())
        if ok and canonical_p:
            phone_h = hash_phone(canonical_p)
            phone_m = mask_phone(canonical_p)

    payload = data.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    payload_json = json.dumps(payload, ensure_ascii=False)

    # Disposition validation
    disp_code: Optional[str] = None
    is_unknown_disp = 0

    if event_type.startswith("disposition."):
        raw_code = payload.get("disposition_code")
        if raw_code and isinstance(raw_code, str):
            code_candidate = raw_code.strip()
            wf_disps = dispositions.get(workflow_id, {})
            if code_candidate in wf_disps:
                disp_code = code_candidate
            else:
                # Unknown disposition code for this workflow
                is_unknown_disp = 1
                disp_code = None  # Excluded from effective disposition
        else:
            is_unknown_disp = 1

    parsed = ParsedEvent(
        event_id=event_id,
        workflow_id=workflow_id,
        event_type=event_type,
        call_id=call_id,
        occurred_at_utc=utc_iso,
        occurred_at_ist=ist_iso,
        call_date_ist=ist_date,
        phone_hash=phone_h,
        phone_masked=phone_m,
        payload_json=payload_json,
        disposition_code=disp_code,
        is_unknown_disposition=is_unknown_disp,
    )
    return "valid", parsed


def ingest_event_jsonl(
    jsonl_path: Path,
    conn: sqlite3.Connection,
    batch_key_prefix: str = "",
    force: bool = False,
) -> Dict[str, int]:
    """Ingest a single JSON Lines event file transactionally.
    
    Rules:
    - events_total counts every non-empty line.
    - Malformed lines are counted in malformed_events and skipped.
    - Duplicate event_ids are counted in duplicate_events_dropped and skipped (never overwrite).
    - Unknown disposition codes are counted in unknown_disposition_events and ignored.
    - Idempotent re-ingestion.
    """
    dispositions = load_dispositions()
    file_key = f"{batch_key_prefix}/events/{jsonl_path.name}".lstrip("/")
    checksum = compute_file_checksum(jsonl_path)

    # Check if already ingested with identical checksum (Idempotency)
    existing_batch = conn.execute(
        "SELECT file_checksum FROM ingested_batches WHERE batch_key = ?",
        (file_key,),
    ).fetchone()

    if existing_batch and existing_batch["file_checksum"] == checksum and not force:
        existing_metrics = conn.execute(
            """
            SELECT events_total, malformed_events, duplicate_events_dropped, unknown_disposition_events
            FROM file_metrics WHERE file_key = ?
            """,
            (file_key,),
        ).fetchone()
        if existing_metrics:
            return dict(existing_metrics)

    now_iso = datetime.now(timezone.utc).isoformat()
    events_total = 0
    malformed_events = 0
    duplicate_events_dropped = 0
    unknown_disposition_events = 0

    # In-memory tracking of event_ids seen in this file run
    seen_in_batch: Set[str] = set()

    # Query already stored event IDs in DB to detect cross-batch duplicates fast
    existing_db_events: Set[str] = {
        row["event_id"]
        for row in conn.execute("SELECT event_id FROM interaction_ledger").fetchall()
    }

    # Pre-load existing leads cache: (workflow_id, phone_hash) -> lead_id
    leads_cache: Dict[Tuple[str, str], int] = {
        (row["workflow_id"], row["phone_hash"]): row["id"]
        for row in conn.execute("SELECT id, workflow_id, phone_hash FROM leads").fetchall()
    }

    # Pre-load call_id to lead_id map for calls already linked
    call_lead_cache: Dict[str, int] = {
        row["call_id"]: row["lead_id"]
        for row in conn.execute("SELECT DISTINCT call_id, lead_id FROM interaction_ledger WHERE lead_id IS NOT NULL").fetchall()
    }

    rows_to_insert: List[Tuple] = []

    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            status, parsed = parse_event_line(line, dispositions)
            if status == "empty":
                # Empty lines are skipped and not counted in events_total
                continue

            # Non-empty lines count in events_total
            events_total += 1

            if status == "malformed" or parsed is None:
                malformed_events += 1
                continue

            # Check duplicate event_id (within batch or previously stored in DB)
            if parsed.event_id in seen_in_batch or parsed.event_id in existing_db_events:
                duplicate_events_dropped += 1
                # Skip duplicate; never overwrite existing event
                continue

            seen_in_batch.add(parsed.event_id)

            if parsed.is_unknown_disposition == 1:
                unknown_disposition_events += 1

            # Determine lead linking
            lead_id: Optional[int] = None
            is_orphan = 1

            if parsed.phone_hash:
                lead_id = leads_cache.get((parsed.workflow_id, parsed.phone_hash))
                if lead_id is not None:
                    is_orphan = 0
                    call_lead_cache[parsed.call_id] = lead_id
            elif parsed.call_id in call_lead_cache:
                # Link via existing call event (e.g. for disposition.override)
                lead_id = call_lead_cache[parsed.call_id]
                is_orphan = 0

            rows_to_insert.append(
                (
                    parsed.event_id,
                    parsed.workflow_id,
                    parsed.event_type,
                    parsed.call_id,
                    parsed.occurred_at_utc,
                    parsed.occurred_at_ist,
                    parsed.call_date_ist,
                    parsed.phone_hash,
                    parsed.phone_masked,
                    parsed.payload_json,
                    parsed.disposition_code,
                    parsed.is_unknown_disposition,
                    lead_id,
                    is_orphan,
                    file_key,
                    now_iso,
                )
            )

    # Batch insert new events into interaction_ledger
    if rows_to_insert:
        conn.executemany(
            """
            INSERT INTO interaction_ledger (
                event_id, workflow_id, event_type, call_id,
                occurred_at_utc, occurred_at_ist, call_date_ist,
                phone_hash, phone_masked, payload_json, disposition_code,
                is_unknown_disposition, lead_id, is_orphan, batch_key, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows_to_insert,
        )

    # Reconcile any overrides or events that arrived out-of-order within this batch
    reconcile_orphan_events(conn)

    # Record / update file_metrics
    conn.execute(
        """
        INSERT OR REPLACE INTO file_metrics (
            file_key, file_type, workflow_id, batch_id,
            events_total, malformed_events, duplicate_events_dropped,
            unknown_disposition_events, created_at, updated_at
        ) VALUES (?, 'events', NULL, NULL, ?, ?, ?, ?, ?, ?)
        """,
        (
            file_key,
            events_total,
            malformed_events,
            duplicate_events_dropped,
            unknown_disposition_events,
            now_iso,
            now_iso,
        ),
    )

    # Record in ingested_batches
    conn.execute(
        """
        INSERT OR REPLACE INTO ingested_batches (
            batch_key, file_checksum, ingested_at
        ) VALUES (?, ?, ?)
        """,
        (file_key, checksum, now_iso),
    )

    conn.commit()

    return {
        "events_total": events_total,
        "malformed_events": malformed_events,
        "duplicate_events_dropped": duplicate_events_dropped,
        "unknown_disposition_events": unknown_disposition_events,
    }


def ingest_events_from_drop(drop_dir: Path, conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Scan <drop_dir>/events/*.jsonl in alphabetical order, ingest, and reconcile."""
    results: List[Dict[str, Any]] = []
    events_base = drop_dir / "events"
    if not events_base.exists():
        return results

    drop_name = drop_dir.name  # e.g. 'drop_1'
    jsonl_files = sorted(events_base.glob("*.jsonl"), key=lambda p: p.name)

    for jsonl_file in jsonl_files:
        metrics = ingest_event_jsonl(
            jsonl_path=jsonl_file,
            conn=conn,
            batch_key_prefix=drop_name,
        )
        results.append({"file": str(jsonl_file), **metrics})

    # Final orphan reconciliation pass
    reconcile_orphan_events(conn)

    return results
