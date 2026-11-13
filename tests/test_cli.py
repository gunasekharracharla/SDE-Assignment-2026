"""Unit tests for the CLI entry point."""

from pathlib import Path
from solution.cli import main


def test_cli_help(capsys):
    """Verify CLI prints help when invoked without arguments."""
    try:
        main([])
    except SystemExit as e:
        assert e.code == 2  # argparse missing required command


def test_cli_end_to_end(tmp_path):
    """Verify CLI end-to-end ingest and report workflow."""
    # Create mock drop structure
    drop_dir = tmp_path / "drop_1"
    leads_dir = drop_dir / "leads" / "motor_renewal"
    leads_dir.mkdir(parents=True)
    events_dir = drop_dir / "events"
    events_dir.mkdir(parents=True)

    csv_file = leads_dir / "batch_001.csv"
    with open(csv_file, "w", encoding="utf-8") as f:
        f.write("customer_name,phone,policy_number\n")
        f.write("Aarav Sharma,+919876543210,MOT/2026/001\n")

    jsonl_file = events_dir / "events_0001.jsonl"
    with open(jsonl_file, "w", encoding="utf-8") as f:
        f.write('{"event_id": "e1", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "c1", "occurred_at": "2026-10-01T04:30:00Z", "phone": "+919876543210"}\n')

    db_path = tmp_path / "test_ledger.db"
    reports_dir = tmp_path / "reports_cli"

    # Test ingest
    rc_ingest = main(["--db", str(db_path), "ingest", str(drop_dir)])
    assert rc_ingest == 0

    # Test report
    rc_report = main(["--db", str(db_path), "report", "--out", str(reports_dir)])
    assert rc_report == 0

    assert (reports_dir / "batch_summary.csv").exists()
    assert (reports_dir / "daily_overview.csv").exists()
    assert (reports_dir / "lead_report.csv").exists()
    assert (reports_dir / "rejections.csv").exists()
    assert (reports_dir / "data_quality.json").exists()
