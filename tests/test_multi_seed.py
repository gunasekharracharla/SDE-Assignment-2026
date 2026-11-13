"""Comprehensive multi-seed data generation and official pipeline verification.

Validates seeds 42, 100, and 2026 across:
- Data generation using official generate_data.py
- Fresh database isolation
- Ingestion order: drop_1 then drop_2
- 5 output files existence and exact headers/column ordering
- Sorting rules
- Lead bucket invariants: total_leads == yet_to_reach_out + in_progress_telephony_failed + in_progress_dialed + completed
- Daily calls and connectivity_rate formatted to exactly 4 decimals
- Success lead counting
- PII masking and zero PII in rejections
- Final orphan event reconciliation
- Re-ingestion idempotency
"""

import csv
import json
import re
import subprocess
import sys
from pathlib import Path
import pytest

from solution.cli import main as cli_main
from solution.config import REPO_ROOT
from solution.db import get_db_connection


SEEDS_TO_VERIFY = [42, 100, 2026]


def run_generator(seed: int, out_dir: Path) -> None:
    """Run official synthetic data generator."""
    gen_script = REPO_ROOT / "assignment-a-data" / "tools" / "generate_data.py"
    cmd = [
        sys.executable,
        str(gen_script),
        "--seed",
        str(seed),
        "--scale",
        "small",
        "--out",
        str(out_dir),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    assert res.returncode == 0


@pytest.mark.parametrize("seed", SEEDS_TO_VERIFY)
def test_seed_pipeline_end_to_end(seed: int, tmp_path: Path):
    """Execute complete ingestion, reporting, and invariant validation for a given seed."""
    seed_data_dir = tmp_path / f"seed_{seed}_data"
    db_path = tmp_path / f"seed_{seed}_ledger.db"
    reports_dir = tmp_path / f"seed_{seed}_reports"

    # 1. Generate fresh data
    run_generator(seed, seed_data_dir)
    assert (seed_data_dir / "drop_1").exists()
    assert (seed_data_dir / "drop_2").exists()

    # 2. Ingest drop_1 then drop_2 into fresh database
    rc1 = cli_main(["--db", str(db_path), "ingest", str(seed_data_dir / "drop_1")])
    assert rc1 == 0
    rc2 = cli_main(["--db", str(db_path), "ingest", str(seed_data_dir / "drop_2")])
    assert rc2 == 0

    # 3. Generate all five reports
    rc_rep = cli_main(["--db", str(db_path), "report", "--out", str(reports_dir)])
    assert rc_rep == 0

    # 4. Check all five output files exist
    expected_files = [
        "batch_summary.csv",
        "daily_overview.csv",
        "lead_report.csv",
        "rejections.csv",
        "data_quality.json",
    ]
    for fn in expected_files:
        assert (reports_dir / fn).exists(), f"Missing file: {fn}"

    # 5. Validate batch_summary.csv
    with open(reports_dir / "batch_summary.csv", "r", encoding="utf-8") as f:
        reader = list(csv.reader(f))
        header = reader[0]
        assert header == [
            "workflow_id",
            "batch_id",
            "total_leads",
            "yet_to_reach_out",
            "in_progress_telephony_failed",
            "in_progress_dialed",
            "completed",
        ]
        data_rows = reader[1:]
        assert len(data_rows) > 0

        # Verify sorting: workflow_id, batch_id
        sort_keys = [(r[0], r[1]) for r in data_rows]
        assert sort_keys == sorted(sort_keys), "batch_summary.csv must be sorted by workflow_id, batch_id"

        # Verify lead bucket invariants
        total_leads_in_summary = 0
        for r in data_rows:
            wf, bid, tot, ytro, iptf, ipd, comp = r[0], r[1], int(r[2]), int(r[3]), int(r[4]), int(r[5]), int(r[6])
            assert tot == ytro + iptf + ipd + comp, (
                f"Bucket invariant violated in {wf}/{bid}: {tot} != {ytro}+{iptf}+{ipd}+{comp}"
            )
            total_leads_in_summary += tot

    # 6. Validate daily_overview.csv
    with open(reports_dir / "daily_overview.csv", "r", encoding="utf-8") as f:
        reader = list(csv.reader(f))
        header = reader[0]
        assert header == [
            "workflow_id",
            "date",
            "total_calls",
            "connected_calls",
            "connectivity_rate",
            "success_leads",
        ]
        data_rows = reader[1:]
        assert len(data_rows) > 0

        # Verify sorting: workflow_id, date
        daily_sort_keys = [(r[0], r[1]) for r in data_rows]
        assert daily_sort_keys == sorted(daily_sort_keys), "daily_overview.csv must be sorted by workflow_id, date"

        for r in data_rows:
            wf, dt, tot_c, conn_c, conn_rate, succ_l = r[0], r[1], int(r[2]), int(r[3]), r[4], int(r[5])
            # Only rows with calls
            assert tot_c > 0, "Daily overview must only include dates with >= 1 call"
            assert conn_c <= tot_c, "Connected calls cannot exceed total calls"
            assert succ_l <= tot_c, "Success leads cannot exceed total calls"
            # Exactly 4 decimal places
            assert re.match(r"^\d+\.\d{4}$", conn_rate), f"Connectivity rate {conn_rate} must have exactly 4 decimal places"
            expected_rate = f"{conn_c / tot_c:.4f}"
            assert conn_rate == expected_rate

    # 7. Validate lead_report.csv
    with open(reports_dir / "lead_report.csv", "r", encoding="utf-8") as f:
        reader = list(csv.reader(f))
        header = reader[0]
        assert header == [
            "workflow_id",
            "external_ref",
            "batch_id",
            "phone_masked",
            "bucket",
            "total_calls",
            "connected_calls",
            "last_call_at",
            "current_disposition",
            "ever_success",
        ]
        data_rows = reader[1:]
        assert len(data_rows) == total_leads_in_summary, "Lead report row count must match batch summary total leads"

        # Verify sorting: workflow_id, external_ref
        lead_sort_keys = [(r[0], r[1]) for r in data_rows]
        assert lead_sort_keys == sorted(lead_sort_keys), "lead_report.csv must be sorted by workflow_id, external_ref"

        valid_buckets = {"yet_to_reach_out", "completed", "in_progress_telephony_failed", "in_progress_dialed"}
        for r in data_rows:
            wf, ext_ref, bid, p_mask, bucket, tot_c, conn_c, last_c, cur_disp, ever_succ = (
                r[0], r[1], r[2], r[3], r[4], int(r[5]), int(r[6]), r[7], r[8], r[9]
            )
            # Masked phone check: +91******XXXX
            assert re.match(r"^\+91\*{6}\d{4}$", p_mask), f"Invalid masked phone format: {p_mask}"
            assert bucket in valid_buckets
            assert ever_succ in {"true", "false"}
            if tot_c > 0:
                assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+05:30$", last_c), (
                    f"Invalid last_call_at format: {last_c}"
                )
            else:
                assert last_c == ""
                assert cur_disp == ""

    # 8. Validate rejections.csv
    with open(reports_dir / "rejections.csv", "r", encoding="utf-8") as f:
        reader = list(csv.reader(f))
        header = reader[0]
        assert header == ["workflow_id", "batch_id", "row_number", "reason"]
        data_rows = reader[1:]

        # Verify sorting: workflow_id, batch_id, row_number numerically
        rej_sort_keys = [(r[0], r[1], int(r[2])) for r in data_rows]
        assert rej_sort_keys == sorted(rej_sort_keys), "rejections.csv must be sorted by workflow_id, batch_id, row_number"

        for r in data_rows:
            reason = r[3]
            assert reason.startswith("missing_required:") or reason == "invalid_phone"

    # 9. Validate data_quality.json
    with open(reports_dir / "data_quality.json", "r", encoding="utf-8") as f:
        dq = json.load(f)
        required_keys = [
            "lead_rows_total",
            "lead_rows_rejected",
            "rejections_by_reason",
            "duplicate_lead_rows_merged",
            "field_warnings",
            "events_total",
            "malformed_events",
            "duplicate_events_dropped",
            "orphan_events",
            "unknown_disposition_events",
        ]
        for k in required_keys:
            assert k in dq, f"Missing key in data_quality.json: {k}"

        assert dq["lead_rows_rejected"] == len(data_rows)
        assert sum(dq["rejections_by_reason"].values()) == dq["lead_rows_rejected"]
        assert dq["lead_rows_total"] == total_leads_in_summary + dq["lead_rows_rejected"] + dq["duplicate_lead_rows_merged"]

        # Verify orphan count matches DB
        with get_db_connection(db_path) as conn:
            actual_orphans = conn.execute("SELECT COUNT(*) AS c FROM interaction_ledger WHERE is_orphan = 1").fetchone()["c"]
            assert dq["orphan_events"] == actual_orphans

    # 10. Verify Idempotency on repeated drop ingestion
    snapshot_before = {
        fn: (reports_dir / fn).read_text(encoding="utf-8")
        for fn in expected_files
    }

    rc1_again = cli_main(["--db", str(db_path), "ingest", str(seed_data_dir / "drop_1")])
    assert rc1_again == 0
    rc2_again = cli_main(["--db", str(db_path), "ingest", str(seed_data_dir / "drop_2")])
    assert rc2_again == 0
    rc_rep_again = cli_main(["--db", str(db_path), "report", "--out", str(reports_dir)])
    assert rc_rep_again == 0

    snapshot_after = {
        fn: (reports_dir / fn).read_text(encoding="utf-8")
        for fn in expected_files
    }

    # Reports must be 100% byte-for-byte identical after re-running
    for fn in expected_files:
        assert snapshot_before[fn] == snapshot_after[fn], f"Idempotency failure: {fn} changed after re-ingest!"
