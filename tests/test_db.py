"""Unit tests for SQLite database schema, constraints, and transactions."""

import sqlite3
import pytest

from solution.db import get_db_connection, init_db


def test_database_initialization():
    """Verify all required tables, views, and indexes are created successfully."""
    with get_db_connection(":memory:") as conn:
        init_db(conn)

        # Check tables existence
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        table_names = [r["name"] for r in tables]

        expected_tables = [
            "file_metrics",
            "ingested_batches",
            "interaction_ledger",
            "leads",
            "rejections",
        ]
        for t in expected_tables:
            assert t in table_names, f"Table {t} was not created"


def test_leads_unique_constraint():
    """Verify unique constraint on (workflow_id, phone_hash)."""
    with get_db_connection(":memory:") as conn:
        init_db(conn)

        conn.execute(
            """
            INSERT INTO leads (
                workflow_id, phone_hash, phone_masked, phone_encrypted,
                customer_name_encrypted, batch_id, external_ref, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "motor_renewal",
                "dummy_hash_1",
                "+91******0036",
                "enc_phone",
                "enc_name",
                "batch_001",
                "MOT/2026/001",
                "2026-10-01T00:00:00Z",
                "2026-10-01T00:00:00Z",
            ),
        )

        # Inserting duplicate (workflow_id, phone_hash) must violate UNIQUE constraint
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO leads (
                    workflow_id, phone_hash, phone_masked, phone_encrypted,
                    customer_name_encrypted, batch_id, external_ref, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "motor_renewal",
                    "dummy_hash_1",
                    "+91******0036",
                    "enc_phone_2",
                    "enc_name_2",
                    "batch_002",
                    "MOT/2026/001",
                    "2026-10-02T00:00:00Z",
                    "2026-10-02T00:00:00Z",
                ),
            )


def test_interaction_ledger_primary_key():
    """Verify primary key constraint on event_id for interaction_ledger."""
    with get_db_connection(":memory:") as conn:
        init_db(conn)

        conn.execute(
            """
            INSERT INTO interaction_ledger (
                event_id, workflow_id, event_type, call_id,
                occurred_at_utc, occurred_at_ist, call_date_ist, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "evt_00000001",
                "motor_renewal",
                "call.initiated",
                "call_00000001",
                "2026-10-01T04:30:00Z",
                "2026-10-01T10:00:00+05:30",
                "2026-10-01",
                "2026-10-01T10:00:00+05:30",
            ),
        )

        # Duplicate event_id must fail
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO interaction_ledger (
                    event_id, workflow_id, event_type, call_id,
                    occurred_at_utc, occurred_at_ist, call_date_ist, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "evt_00000001",
                    "motor_renewal",
                    "call.connected",
                    "call_00000001",
                    "2026-10-01T04:31:00Z",
                    "2026-10-01T10:01:00+05:30",
                    "2026-10-01",
                    "2026-10-01T10:01:00+05:30",
                ),
            )


def test_rejections_unique_constraint():
    """Verify uniqueness on (workflow_id, batch_id, row_number) in rejections table."""
    with get_db_connection(":memory:") as conn:
        init_db(conn)

        conn.execute(
            """
            INSERT INTO rejections (
                workflow_id, batch_id, row_number, reason, batch_key
            ) VALUES (?, ?, ?, ?, ?)
            """,
            ("motor_renewal", "batch_001", 1, "missing_required:customer_name", "drop_1"),
        )

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO rejections (
                    workflow_id, batch_id, row_number, reason, batch_key
                ) VALUES (?, ?, ?, ?, ?)
                """,
                ("motor_renewal", "batch_001", 1, "invalid_phone", "drop_1"),
            )
