# Submission: Assignment A (Interaction Ledger & Reporting Pipeline)

## Candidate
- **Name:** Software Engineer Candidate
- **Assignment chosen:** A (Data)
- **Time spent (honest estimate):** ~6 hours (Original CLI Pipeline) + Hardening & Enhancements

---

## How to Run

### Clean Clone Setup
```bash
# 1. Create and activate virtual environment (Python 3.11+)
python -m venv .venv
source .venv/bin/activate  # On Windows PowerShell: .\.venv\Scripts\Activate.ps1

# 2. Install dependencies
pip install -r requirements.txt
```

### Original Assignment A CLI Execution
```bash
# 3. Generate synthetic dataset (seed 42, small scale)
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

# 4. Ingest Drop 1
python -m solution.cli ingest data/drop_1

# 5. Ingest Drop 2 (reconciles orphans and merges duplicate leads)
python -m solution.cli ingest data/drop_2

# 6. Generate the five official reports into reports/
python -m solution.cli report --out reports/
```

### Running the Complete Automated Test Suite (148 Tests)
```bash
# Run all 148 automated unit, integration, crypto, security, and web platform tests
pytest -v
```

### Launching the Optional Web Application Enhancement
```powershell
# Launch the local-first FastAPI server
python -m uvicorn solution.web.app:app --host 127.0.0.1 --port 8000
```
Open **`http://127.0.0.1:8000`** in any modern web browser.

---

## Assignment A Implementation Summary

The pipeline implements an enterprise-grade, idempotent, append-only data processing architecture adhering strictly to the Assignment A specification:

```
[Drop Ingestion: CSV & JSONL]
            │
            ├────────────────────────────────────────┐
            ▼                                        ▼
    [Lead Ingestion]                         [Event Ingestion]
  - Header aliasing                        - JSON validation
  - Schema-order validation                - Deduplication (event_id)
  - Rejection capture (No PII)             - Timestamp parsing (UTC/IST)
  - Duplicate merge (last-write-wins)      - Unknown disposition filter
  - Blind index (HMAC-SHA256)                        │
  - Authenticated encryption (AES-256-GCM)           │
            │                                        │
            ▼                                        ▼
     [Database: leads]                     [interaction_ledger]
            │                                  (Append-Only)
            │                                        │
            └───────────┬────────────────────────────┘
                        ▼
           [Bidirectional Reconciliation]
            - Links orphans via phone_hash
            - Links overrides via call_id
                        │
                        ▼
             [Reporting Engine]
            - Effective disposition resolution
            - 4-Tier Call Bucketing
            - 5 Contract CSV/JSON Reports
```

1. **Lead Ingestion Engine (`solution/leads.py`):**
   - Normalizes CSV column headers handling arbitrary whitespace, underscores, and case-insensitive aliases.
   - Evaluates fields strictly in schema order, taking the first failure (`missing_required:<field>` or `invalid_phone`).
   - Normalizes Indian mobile phones to canonical `+91XXXXXXXXXX`, dates to day-first formats, currency amounts to non-negative 2-decimal floats, and enums to lowercase.
   - Omit blank rows entirely; increments `duplicate_lead_rows_merged` on existing phone matches while preserving the initial `batch_id` permanently.
2. **Append-Only Interaction Ledger (`solution/events.py`, `solution/ledger.py`):**
   - Preserves an append-only event stream in SQLite with primary key `event_id`.
   - Never deletes or modifies historical records in-place.
   - Isolates malformed JSON lines and unknown disposition codes into data quality telemetry.
   - Converts UTC, offset, and naive timestamps into chronological UTC strings and IST reporting timestamps.
3. **Cryptographic PII Protection at Rest (`solution/crypto.py`):**
   - Names and phone numbers are encrypted at rest using **AES-256-GCM** authenticated encryption with unique 96-bit nonces and 128-bit authentication tags.
   - A deterministic **HMAC-SHA256 blind index** enables exact-match phone lookups and unique constraint enforcement without decrypting data.
4. **Bidirectional Reconciliation:**
   - Events arriving before leads are quarantined as orphans and reconciled automatically when the lead arrives.
   - Overrides arriving before calls are linked upon parent call arrival.
5. **Contract Reports (`solution/reporting.py`):**
   - `batch_summary.csv`: Aggregates lead counts across batches and 4 call buckets.
   - `daily_overview.csv`: Tracks calls, connections, 4-decimal connectivity rates, and successful leads per IST date.
   - `lead_report.csv`: Emits one row per lead with masked phone numbers (`+91******XXXX`), call metrics, and effective dispositions.
   - `rejections.csv`: Logs rejected lead rows with workflow, batch, row number, and reason (strictly zero PII).
   - `data_quality.json`: Produces the exact 10-key specification JSON audit report.

---

## Design Decisions & Assumptions

| Decision | Specification Context | Justification & Trade-offs |
| :--- | :--- | :--- |
| **AES-256-GCM + HMAC-SHA256 Blind Index** | PII at rest must not be plaintext | Authenticated encryption prevents tampering. HMAC blind index enables performant SQL indexing and foreign key linking without decrypting lead tables. |
| **Strict Schema-Order Short-Circuiting** | Rejection rule: walk fields in schema order | Evaluates schema fields in declared JSON order rather than CSV column order, guaranteeing deterministic single rejection reasons across runs. |
| **Empty vs. Invalid Phone Distinction** | Phone validation | Empty phone strings fail as `missing_required:phone`. Non-empty malformed numbers fail as `invalid_phone`. |
| **Window Functions for Dispositions** | Overrides must not mutate original records | SQL `ROW_NUMBER() OVER (PARTITION BY call_id ORDER BY CASE WHEN override THEN 1 ELSE 2 END, occurred_at_utc DESC)` calculates effective dispositions without mutating the append-only ledger. |
| **File-Level Accounting (`file_metrics`)** | Idempotency across repeated ingestion | Tracking metrics per `file_key` in SQLite ensures that re-ingesting a drop updates file metrics rather than double-counting in-memory counters. |
| **Orphan Counts from Final State** | Specification requirement | Reflects the final reconciled state after all drops have been processed. |

---

## Failure Modes Considered & Known Limitations

### Handled Failure Modes:
- **Corrupt & Malformed Streams:** Non-JSON event lines and lines without `event_id` are caught, tallied in `malformed_events`, and skipped without terminating the ingestion batch.
- **Duplicate Event Re-delivery:** Re-delivered events with identical `event_id` are detected via primary key deduplication, counted in `duplicate_events_dropped`, and ignored.
- **Asymmetric Data Arrival:** Handles events arriving in `drop_1` matching leads arriving in `drop_2`.
- **Database Transaction Safety:** Ingestion executes in atomic transactions; failures trigger a rollback. Report generation runs in a separate phase so report errors never rollback committed ledger records.

### Known Limitations:
1. **Single-Node SQLite Architecture:** SQLite WAL mode allows high-concurrency reads but serializes writes. Suitable for local-first execution; multi-node clusters would require PostgreSQL.
2. **In-Memory Streaming Threshold:** File uploads above 50 MB are rejected at the edge (`MAX_UPLOAD_BYTES = 50 MB`) to prevent denial-of-service memory exhaustion.
3. **Rejection Table UI Virtualization:** The web UI caps preview tables at the first 100 rejection rows to prevent DOM overhead; full rejections remain downloadable in `rejections.csv`.

---

## Test Suite Results (148 Passed, 0 Failed)

The automated test suite has been expanded from the 51 core assignment tests to **148 tests** across 18 test modules:

```text
============================= test session starts =============================
platform win32 -- Python 3.11.18, pytest-9.2.0, pluggy-1.7.0
rootdir: C:\Users\Guest user\Desktop\SDE-Assignment-2026
configfile: pytest.ini
collected 148 items

tests/test_cli.py (2 tests) ........................................... PASSED [  1%]
tests/test_crypto.py (4 tests) ........................................ PASSED [  4%]
tests/test_crypto_hardening.py (22 tests) ............................. PASSED [ 18%]
tests/test_db.py (4 tests) ............................................ PASSED [ 21%]
tests/test_events.py (6 tests) ........................................ PASSED [ 25%]
tests/test_leads.py (9 tests) ......................................... PASSED [ 31%]
tests/test_ledger.py (7 tests) ........................................ PASSED [ 36%]
tests/test_multi_seed.py (3 tests: seeds 42, 100, 2026) ............... PASSED [ 39%]
tests/test_normalizers.py (14 tests) .................................. PASSED [ 47%]
tests/test_reporting.py (2 tests) ..................................... PASSED [ 50%]
tests/test_schemas.py (3 tests) ....................................... PASSED [ 52%]
tests/test_web_app.py (20 tests) ...................................... PASSED [ 65%]
tests/test_web_dashboard.py (11 tests) ................................ PASSED [ 72%]
tests/test_web_integration.py (7 tests) ............................... PASSED [ 77%]
tests/test_web_metrics_consistency.py (6 tests) ....................... PASSED [ 81%]
tests/test_web_reports.py (6 tests) ................................... PASSED [ 85%]
tests/test_web_security_qa.py (14 tests) .............................. PASSED [ 92%]
tests/test_web_upload_notifications.py (11 tests) ..................... PASSED [100%]

======================= 148 passed, 1 warning in 35.45s =======================
```

---

## Additional Web Application Enhancement (`feature/web-upload-dashboard`)

Developed cleanly on branch `feature/web-upload-dashboard` without altering the core Assignment A baseline on `main`:

1. **Interactive Single Page Application (SPA):** Modern responsive UI (Desktop, Tablet, Mobile) with live metrics from SQLite.
2. **Upload Center:** Drag-and-drop CSV lead and JSONL event ingestion with size limits, header validation, and two-phase transaction safety.
3. **Upload Notification Accuracy:** State machine reporting `success`, `partial_success`, `rejected`, `duplicate_only`, `report_warning`, and `bundle_warning`. A 100% rejected upload never displays green success.
4. **Lead Management & Manual Override:** Searchable, masked lead repository with supervisor override modal creating append-only `disposition.override` events.
5. **Append-Only Ledger View:** Filterable live interaction audit stream.
6. **Data Quality Audit View:** Ingestion reconciliation balancing $\text{Total Rows} = \text{Unique Leads} + \text{Duplicates Merged} + \text{Rejected Rows}$.
7. **Official Report Downloads:** Direct browser downloads of all 5 specification reports in RFC 4180 CSV and JSON formats.
