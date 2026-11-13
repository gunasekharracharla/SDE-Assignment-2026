"""Database connection, schema definitions, and migration utilities."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from solution.config import get_db_path

DDL_STATEMENTS = """
-- Track ingested files for idempotency
CREATE TABLE IF NOT EXISTS ingested_batches (
    batch_key TEXT PRIMARY KEY,
    file_checksum TEXT NOT NULL,
    ingested_at TEXT NOT NULL
);

-- Master table for validated leads (PII encrypted / hashed at rest)
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL,
    phone_hash TEXT NOT NULL,
    phone_masked TEXT NOT NULL,
    phone_encrypted TEXT NOT NULL,
    customer_name_encrypted TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    external_ref TEXT NOT NULL,
    preferred_language TEXT,
    extra_data TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(workflow_id, phone_hash)
);

CREATE INDEX IF NOT EXISTS idx_leads_external_ref 
    ON leads(workflow_id, external_ref);
CREATE INDEX IF NOT EXISTS idx_leads_batch 
    ON leads(workflow_id, batch_id);

-- Append-only interaction ledger for all call events
CREATE TABLE IF NOT EXISTS interaction_ledger (
    event_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    call_id TEXT NOT NULL,
    occurred_at_utc TEXT NOT NULL,
    occurred_at_ist TEXT NOT NULL,
    call_date_ist TEXT NOT NULL,
    phone_hash TEXT,
    phone_masked TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    disposition_code TEXT,
    is_unknown_disposition INTEGER NOT NULL DEFAULT 0,
    lead_id INTEGER REFERENCES leads(id) ON DELETE SET NULL,
    is_orphan INTEGER NOT NULL DEFAULT 1,
    batch_key TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ledger_call_lookup 
    ON interaction_ledger(call_id, occurred_at_utc);
CREATE INDEX IF NOT EXISTS idx_ledger_lead 
    ON interaction_ledger(lead_id);
CREATE INDEX IF NOT EXISTS idx_ledger_orphan 
    ON interaction_ledger(is_orphan, workflow_id, phone_hash);
CREATE INDEX IF NOT EXISTS idx_ledger_call_date 
    ON interaction_ledger(workflow_id, call_date_ist, event_type);

-- Rejected lead rows (Strictly NO PII stored)
CREATE TABLE IF NOT EXISTS rejections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    row_number INTEGER NOT NULL,
    reason TEXT NOT NULL,
    batch_key TEXT NOT NULL,
    UNIQUE(workflow_id, batch_id, row_number)
);

CREATE INDEX IF NOT EXISTS idx_rejections_ordering 
    ON rejections(workflow_id, batch_id, row_number);

-- File-level metrics tracked per file for 100% idempotent data quality reporting
CREATE TABLE IF NOT EXISTS file_metrics (
    file_key TEXT PRIMARY KEY,
    file_type TEXT NOT NULL,
    workflow_id TEXT,
    batch_id TEXT,
    lead_rows_total INTEGER NOT NULL DEFAULT 0,
    lead_rows_rejected INTEGER NOT NULL DEFAULT 0,
    duplicate_lead_rows_merged INTEGER NOT NULL DEFAULT 0,
    field_warnings INTEGER NOT NULL DEFAULT 0,
    events_total INTEGER NOT NULL DEFAULT 0,
    malformed_events INTEGER NOT NULL DEFAULT 0,
    duplicate_events_dropped INTEGER NOT NULL DEFAULT 0,
    unknown_disposition_events INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def init_db(conn: sqlite3.Connection) -> None:
    """Initialize database tables, indices, and constraints."""
    with conn:
        conn.executescript(DDL_STATEMENTS)


def configure_connection(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Apply performance, constraint, and collation configurations."""
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA busy_timeout = 10000;")
    return conn


@contextmanager
def get_db_connection(db_path: Path | str | None = None) -> Generator[sqlite3.Connection, None, None]:
    """Provide a transactional SQLite database connection context."""
    path = db_path if db_path is not None else get_db_path()
    
    # If path is not in-memory, ensure parent directories exist
    if str(path) != ":memory:":
        target_path = Path(path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(target_path))
    else:
        conn = sqlite3.connect(":memory:")

    configure_connection(conn)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

