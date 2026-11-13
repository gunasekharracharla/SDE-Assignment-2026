"""Command-line interface for the data ingestion and reporting pipeline."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from solution.config import get_db_path
from solution.db import get_db_connection, init_db
from solution.events import ingest_events_from_drop
from solution.leads import ingest_leads_from_drop
from solution.ledger import reconcile_orphan_events
from solution.reporting import generate_all_reports


def handle_ingest(args: argparse.Namespace) -> int:
    """Execute ingestion for a drop directory."""
    drop_dir = Path(args.drop_path).resolve()
    if not drop_dir.exists():
        print(f"Error: Drop directory does not exist: {drop_dir}", file=sys.stderr)
        return 1

    db_path = Path(args.db) if args.db else get_db_path()

    with get_db_connection(db_path) as conn:
        init_db(conn)

        # 1. Ingest Leads
        lead_results = ingest_leads_from_drop(drop_dir, conn)
        # 2. Ingest Events
        event_results = ingest_events_from_drop(drop_dir, conn)
        # 3. Final Reconciliation
        resolved_count = reconcile_orphan_events(conn)

    print(f"Ingested {len(lead_results)} lead files and {len(event_results)} event files from {drop_dir.name}.")
    if resolved_count > 0:
        print(f"Reconciled {resolved_count} orphan events.")
    return 0


def handle_report(args: argparse.Namespace) -> int:
    """Generate all five report files into the output directory."""
    out_dir = Path(args.out).resolve()
    db_path = Path(args.db) if args.db else get_db_path()

    with get_db_connection(db_path) as conn:
        init_db(conn)
        report_paths = generate_all_reports(conn, out_dir)

    print(f"Successfully generated all five reports into {out_dir}:")
    for name, p in report_paths.items():
        print(f"  - {p.name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Agentic Universe - Interaction Ledger & Reporting Pipeline",
    )
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="Path to SQLite database (defaults to data/ledger.db or LEDGER_DB_PATH)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # Ingest command
    ingest_parser = subparsers.add_parser("ingest", help="Ingest a data drop directory")
    ingest_parser.add_argument(
        "drop_path",
        type=str,
        help="Path to drop directory (e.g. data/drop_1)",
    )
    ingest_parser.set_defaults(func=handle_ingest)

    # Report command
    report_parser = subparsers.add_parser("report", help="Generate all reporting files")
    report_parser.add_argument(
        "--out",
        type=str,
        required=True,
        help="Directory to write report files into",
    )
    report_parser.set_defaults(func=handle_report)

    parsed = parser.parse_args(argv)
    return parsed.func(parsed)


if __name__ == "__main__":
    sys.exit(main())
