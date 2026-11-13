"""Lead CSV ingestion, schema-order validation, rejection handling, and deduplication."""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import sqlite3

from solution.config import UTC, compute_file_checksum
from solution.crypto import encrypt_pii, hash_phone, mask_phone
from solution.ledger import reconcile_orphan_events
from solution.normalizers import (
    normalize_date,
    normalize_decimal,
    normalize_enum,
    normalize_phone,
    normalize_string,
)
from solution.schemas import FieldSchema, WorkflowSchema, load_schemas, normalize_header


@dataclass
class LeadValidationResult:
    is_blank: bool = False
    is_rejected: bool = False
    rejection_reason: Optional[str] = None
    field_warnings: int = 0
    canonical_phone: Optional[str] = None
    customer_name: Optional[str] = None
    external_ref: Optional[str] = None
    preferred_language: Optional[str] = None
    extra_data: Dict[str, Any] = field(default_factory=dict)


def validate_lead_row(
    raw_row: Dict[str, str],
    schema: WorkflowSchema,
    header_map: Dict[str, str],
) -> LeadValidationResult:
    """Validate and normalize a single CSV row strictly in schema order.
    
    Rules:
    1. Blank row (all values empty/whitespace) -> skipped and not counted.
    2. Check fields in schema.fields order:
       - Missing required -> 'missing_required:<field_name>'
       - Invalid phone -> 'invalid_phone'
    3. Invalid optional field -> set to None and count one field warning (only for non-rejected rows).
    """
    # 1. Blank check: is every cell empty or whitespace?
    if not raw_row or all(not str(v).strip() for v in raw_row.values()):
        return LeadValidationResult(is_blank=True)

    # 2. Map raw CSV columns to schema field names using header_map
    row_by_field: Dict[str, str] = {}
    for col_name, raw_val in raw_row.items():
        if col_name is None:
            continue
        norm_col = normalize_header(col_name)
        field_name = header_map.get(norm_col)
        if field_name:
            row_by_field[field_name] = raw_val

    # 3. Walk schema fields strictly in schema order
    warnings = 0
    normalized_values: Dict[str, Any] = {}

    for f in schema.fields:
        raw_val = row_by_field.get(f.name)
        trimmed = raw_val.strip() if raw_val is not None else ""
        is_empty = (trimmed == "")

        # Check required fields
        if f.required and is_empty:
            return LeadValidationResult(
                is_rejected=True,
                rejection_reason=f"missing_required:{f.name}",
            )

        # Handle field types
        if f.type == "phone":
            if is_empty:
                # If required and empty, handled above. If not required and empty:
                normalized_values[f.name] = None
            else:
                ok, canonical_p = normalize_phone(trimmed)
                if not ok:
                    return LeadValidationResult(
                        is_rejected=True,
                        rejection_reason="invalid_phone",
                    )
                normalized_values[f.name] = canonical_p

        elif f.type == "string":
            normalized_values[f.name] = normalize_string(trimmed)

        elif f.type == "date":
            if is_empty:
                normalized_values[f.name] = None
            else:
                ok, d_val = normalize_date(trimmed)
                if ok:
                    normalized_values[f.name] = d_val
                else:
                    normalized_values[f.name] = None
                    warnings += 1

        elif f.type == "decimal":
            if is_empty:
                normalized_values[f.name] = None
            else:
                ok, dec_val = normalize_decimal(trimmed)
                if ok:
                    normalized_values[f.name] = dec_val
                else:
                    normalized_values[f.name] = None
                    warnings += 1

        elif f.type == "enum":
            if is_empty:
                normalized_values[f.name] = None
            else:
                ok, enum_val = normalize_enum(trimmed, f.values)
                if ok:
                    normalized_values[f.name] = enum_val
                else:
                    normalized_values[f.name] = None
                    warnings += 1

    # Extract core columns
    canonical_phone = normalized_values.get("phone")
    customer_name = normalized_values.get("customer_name")
    ref_field = schema.reference_field_name
    external_ref = normalized_values.get(ref_field)
    preferred_lang = normalized_values.get("preferred_language")

    # Extra fields (workflow specific fields like policy_expiry, premium_amount, product, order_date)
    core_keys = {"phone", "customer_name", ref_field, "preferred_language"}
    extra_data = {
        k: v for k, v in normalized_values.items()
        if k not in core_keys and v is not None
    }

    return LeadValidationResult(
        is_rejected=False,
        field_warnings=warnings,
        canonical_phone=canonical_phone,
        customer_name=customer_name,
        external_ref=external_ref,
        preferred_language=preferred_lang,
        extra_data=extra_data,
    )


def compute_file_checksum(file_path: Path) -> str:
    """Compute SHA-256 checksum of a file for idempotency checking."""
    sha = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            sha.update(chunk)
    return sha.hexdigest()


def ingest_lead_csv(
    csv_path: Path,
    workflow_id: str,
    conn: sqlite3.Connection,
    batch_key_prefix: str = "",
    force: bool = False,
) -> Dict[str, int]:
    """Ingest a single lead CSV file transactionally.
    
    Returns metrics dictionary for this file.
    """
    schemas = load_schemas()
    if workflow_id not in schemas:
        raise ValueError(f"Unknown workflow_id: {workflow_id}")
    schema = schemas[workflow_id]
    header_map = schema.build_header_map()

    batch_id = csv_path.stem  # e.g. 'batch_001'
    file_key = f"{batch_key_prefix}/{workflow_id}/{csv_path.name}".lstrip("/")
    checksum = compute_file_checksum(csv_path)

    # Check if already ingested with identical checksum (Idempotency)
    existing_batch = conn.execute(
        "SELECT file_checksum FROM ingested_batches WHERE batch_key = ?",
        (file_key,),
    ).fetchone()

    if existing_batch and existing_batch["file_checksum"] == checksum and not force:
        # Return existing file metrics without re-processing
        existing_metrics = conn.execute(
            """
            SELECT lead_rows_total, lead_rows_rejected, duplicate_lead_rows_merged, field_warnings
            FROM file_metrics WHERE file_key = ?
            """,
            (file_key,),
        ).fetchone()
        if existing_metrics:
            return dict(existing_metrics)

    now_iso = datetime.now(timezone.utc).isoformat()
    lead_rows_total = 0
    lead_rows_rejected = 0
    duplicate_lead_rows_merged = 0
    field_warnings_total = 0

    # Read CSV rows
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        row_number = 0

        for raw_row in reader:
            res = validate_lead_row(raw_row, schema, header_map)
            if res.is_blank:
                # Blank rows are skipped and not counted per specification
                continue

            row_number += 1
            lead_rows_total += 1

            if res.is_rejected:
                lead_rows_rejected += 1
                # Insert rejection record (Strictly NO PII)
                conn.execute(
                    """
                    INSERT OR REPLACE INTO rejections (
                        workflow_id, batch_id, row_number, reason, batch_key
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (workflow_id, batch_id, row_number, res.rejection_reason, file_key),
                )
                continue

            # Non-rejected row: count field warnings
            field_warnings_total += res.field_warnings

            # Lead identity is (workflow_id, canonical phone)
            canonical_phone = res.canonical_phone
            assert canonical_phone is not None
            phone_h = hash_phone(canonical_phone)
            phone_m = mask_phone(canonical_phone)

            # Check if lead already exists in DB
            existing_lead = conn.execute(
                "SELECT id, batch_id, extra_data, preferred_language FROM leads WHERE workflow_id = ? AND phone_hash = ?",
                (workflow_id, phone_h),
            ).fetchone()

            if existing_lead is None:
                # New lead: first batch_id becomes permanent
                phone_enc = encrypt_pii(canonical_phone)
                name_enc = encrypt_pii(res.customer_name)
                extra_json = json.dumps(res.extra_data, ensure_ascii=False)

                conn.execute(
                    """
                    INSERT INTO leads (
                        workflow_id, phone_hash, phone_masked, phone_encrypted,
                        customer_name_encrypted, batch_id, external_ref,
                        preferred_language, extra_data, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        workflow_id,
                        phone_h,
                        phone_m,
                        phone_enc,
                        name_enc,
                        batch_id,
                        res.external_ref,
                        res.preferred_language,
                        extra_json,
                        now_iso,
                        now_iso,
                    ),
                )
            else:
                # Merged duplicate: counts as one merged duplicate
                duplicate_lead_rows_merged += 1
                lead_id = existing_lead["id"]

                # Overwrite only non-null fields (last write wins)
                # Note: batch_id is permanent and NOT overwritten
                updates: List[str] = []
                params: List[Any] = []

                if res.customer_name is not None:
                    updates.append("customer_name_encrypted = ?")
                    params.append(encrypt_pii(res.customer_name))

                if res.external_ref is not None:
                    updates.append("external_ref = ?")
                    params.append(res.external_ref)

                if res.preferred_language is not None:
                    updates.append("preferred_language = ?")
                    params.append(res.preferred_language)

                # Merge extra_data
                existing_extra = json.loads(existing_lead["extra_data"] or "{}")
                if res.extra_data:
                    existing_extra.update(res.extra_data)
                    updates.append("extra_data = ?")
                    params.append(json.dumps(existing_extra, ensure_ascii=False))

                updates.append("updated_at = ?")
                params.append(now_iso)

                params.append(lead_id)
                sql = f"UPDATE leads SET {', '.join(updates)} WHERE id = ?"
                conn.execute(sql, params)

    # Apply schema defaults after merging to fields that remain null
    for f in schema.fields:
        if f.default is not None:
            if f.name == "preferred_language":
                conn.execute(
                    """
                    UPDATE leads
                    SET preferred_language = ?
                    WHERE workflow_id = ? AND preferred_language IS NULL
                    """,
                    (f.default, workflow_id),
                )

    # Record / update file_metrics
    conn.execute(
        """
        INSERT OR REPLACE INTO file_metrics (
            file_key, file_type, workflow_id, batch_id,
            lead_rows_total, lead_rows_rejected, duplicate_lead_rows_merged,
            field_warnings, created_at, updated_at
        ) VALUES (?, 'leads', ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            file_key,
            workflow_id,
            batch_id,
            lead_rows_total,
            lead_rows_rejected,
            duplicate_lead_rows_merged,
            field_warnings_total,
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

    # Reconcile any orphan events that were waiting for leads from this batch
    reconcile_orphan_events(conn)

    conn.commit()

    return {
        "lead_rows_total": lead_rows_total,
        "lead_rows_rejected": lead_rows_rejected,
        "duplicate_lead_rows_merged": duplicate_lead_rows_merged,
        "field_warnings": field_warnings_total,
    }


def ingest_leads_from_drop(drop_dir: Path, conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Scan <drop_dir>/leads/<workflow_id>/*.csv in alphabetical order and ingest."""
    results: List[Dict[str, Any]] = []
    leads_base = drop_dir / "leads"
    if not leads_base.exists():
        return results

    drop_name = drop_dir.name  # e.g. 'drop_1'

    # Process workflows
    wf_dirs = sorted([p for p in leads_base.iterdir() if p.is_dir()])
    for wf_dir in wf_dirs:
        workflow_id = wf_dir.name
        csv_files = sorted(wf_dir.glob("*.csv"), key=lambda p: p.name)
        for csv_file in csv_files:
            metrics = ingest_lead_csv(
                csv_path=csv_file,
                workflow_id=workflow_id,
                conn=conn,
                batch_key_prefix=drop_name,
            )
            results.append({"file": str(csv_file), **metrics})

    return results
