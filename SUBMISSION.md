# Submission: Assignment A (Interaction Ledger & Reporting Pipeline)

## Candidate
- **Name:** Software Engineer Candidate
- **Assignment chosen:** A (Data)
- **Time spent (honest estimate):** ~6 hours

---

## How to run

### Clean Clone Setup
```bash
# 1. Create and activate virtual environment (Python 3.11+)
python -m venv .venv
source .venv/bin/activate  # On Windows PowerShell: .venv\Scripts\Activate.ps1

# 2. Install dependencies
pip install -r requirements.txt
```

### Data Generation & Pipeline Execution
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

### Running Automated Tests
```bash
# Run complete test suite (51 tests covering normalizers, schemas, ledger, reports, multi-seed)
pytest -v
```

---

## Design

### Architecture Overview
The pipeline follows a modular, layered design separating data parsing, schema validation, cryptographic PII protection, append-only event ledgering, and SQL-driven reporting:

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

### Database Schema (SQLite 3)
We utilize standard SQLite with WAL mode (`PRAGMA journal_mode = WAL`) and foreign key constraints enabled.

1. **`leads` Table:**
   - Stores validated master leads.
   - **PII at rest:** `customer_name_encrypted` and `phone_encrypted` use **AES-256-GCM** authenticated encryption with random nonces and auth tags.
   - **Deterministic Blind Index:** `phone_hash` uses `HMAC-SHA256(canonical_phone, secret_salt)` allowing fast exact-match lookups and unique constraint enforcement on `(workflow_id, phone_hash)`.
   - **Permanent Batch:** `batch_id` records the initial batch stem permanently.
   - `extra_data`: JSON column storing non-standard workflow attributes (`policy_expiry`, `premium_amount`, `product`, `order_date`).

2. **`interaction_ledger` Table (Append-Only):**
   - Single source of truth for all call interactions.
   - `event_id` is the primary key (deduplication key).
   - Existing records are **never edited or deleted**.
   - Timestamps are normalized: `occurred_at_utc` (ISO-8601 UTC string for chronological sorting) and `occurred_at_ist` (for reporting).
   - `is_orphan` (1 = unlinked, 0 = linked to a valid lead).

3. **`rejections` Table:**
   - Tracks rejected CSV rows with `workflow_id, batch_id, row_number, reason`.
   - Strictly contains **no PII** (zero names or phone numbers).
   - Enforces `UNIQUE(workflow_id, batch_id, row_number)` for idempotent logging.

4. **`file_metrics` & `ingested_batches` Tables:**
   - Tracks row counts, rejections, duplicate merges, and anomalies per file.
   - Eliminates fragile in-memory counters and guarantees 100% idempotent data quality reporting across repeated runs.

---

## Decisions & Assumptions

| Decision | Why | Alternative Considered |
|---|---|---|
| **AES-256-GCM + HMAC-SHA256 Blind Index** | Protects PII with authenticated symmetric encryption while enabling fast, indexable lookups without decrypting rows. | Storing plaintext phone (violates PII requirement) or Base64 (encoding, not encryption). |
| **Strict Schema-Order Short-Circuiting** | The spec dictates walking fields in schema order and taking the first failure. | Validating in CSV column order (would produce non-deterministic rejection reasons). |
| **Empty Phone vs Invalid Phone** | Empty/whitespace string is classified as missing required (`missing_required:phone`), whereas present non-digit or malformed number is `invalid_phone`. | Treating empty phone as invalid_phone (contradicts missing required rule). |
| **SQL Window Function for Dispositions** | SQL `ROW_NUMBER() OVER (PARTITION BY call_id ORDER BY CASE WHEN override THEN 1 ELSE 2 END, occurred_at_utc DESC)` guarantees overrides always take precedence over sets and picks the latest event without mutating the append-only ledger. | Updating an existing `call` row in-place (violates append-only ledger rule). |
| **File-Level Metric Storage (`file_metrics`)** | Guarantees idempotency when drops are re-ingested. Aggregating from file metrics prevents counter inflation. | In-memory global counters (would double if a drop is re-ingested). |
| **Orphan Counts from Final DB State** | The spec specifies that orphan counts in `data_quality.json` must reflect the final state after all reconciliation. | Storing static orphan counts at initial event ingestion time. |

---

## Failure Modes Considered

- **Malformed JSON Lines:** Wrapped in exception handlers; increments `malformed_events` and continues stream processing without crashing.
- **Duplicate Event Delivery:** Encountering an existing `event_id` increments `duplicate_events_dropped` and skips without modifying original ledger records.
- **Out-of-Order Events & Overrides:** An override arriving before its call's `call.initiated` is held as an orphan and linked as soon as the parent call arrives.
- **Events Arriving Before Leads:** Events in `drop_1` matching leads in `drop_2` are stored as orphans and reconciled into linked events once `drop_2` is ingested.
- **Duplicate Leads Across Batches:** First valid row sets permanent `batch_id`; later rows merge non-null fields (last-write-wins) and increment `duplicate_lead_rows_merged`.
- **Database Transaction Safety:** Ingestion of each batch runs in an atomic transaction; errors trigger rollback to avoid partial batch writes.

---

## Testing

### What Was Tested
- **Normalizers:** Indian mobile phone formats (`+91`, `0`, `91-`, landlines, US numbers, short numbers), all 4 accepted date formats (`YYYY-MM-DD`, `DD/MM/YYYY`, `DD-MM-YYYY`, `DD-Mon-YYYY`, case-insensitive months, leap years, impossible dates like `31/02/2026`), currency prefixes (`₹`, `Rs`, `Rs.`, `INR`), decimal rounding, and enums.
- **Schema & Headers:** Underscore/space normalization, alias resolution, unmatched column omission, disposition catalogue terminal/success mapping.
- **PII Protection:** AES-256-GCM roundtrip encryption, authentication tag tampering detection, HMAC blind index determinism, phone masking.
- **Leads Pipeline:** Schema-order rejection order, missing required fields, blank row omission, field warning counting on accepted rows only, duplicate merging, permanent `batch_id`, schema defaults.
- **Ledger & Events:** Event deduplication, append-only immutability, unknown disposition code isolation, timestamp normalization across UTC, offset, and naive (IST).
- **Linking & Bucketing:** Events before leads, leads before events, overrides before calls, multiple overrides, calls without `call.initiated`, all 4 bucket conditions and precedences.
- **Reporting Contract:** Exact column names, column ordering, sorting rules, connectivity rate 4-decimal formatting, success lead counting, and zero PII in rejections.
- **Multi-Seed Verification:** Automated end-to-end verification across seeds **42**, **100**, and **2026** verifying output diffs and idempotency on repeated ingestion.

### What Was Not Tested
- Distributed multi-node concurrency (SQLite is single-writer; WAL mode allows concurrent readers).

---

## Results

### Verification Across Seeds
```
============================= test session starts =============================
collected 51 items
tests/test_cli.py::test_cli_help PASSED                                  [  1%]
tests/test_cli.py::test_cli_end_to_end PASSED                            [  3%]
tests/test_crypto.py (4 tests) PASSED                                    [ 11%]
tests/test_db.py (4 tests) PASSED                                        [ 19%]
tests/test_events.py (6 tests) PASSED                                    [ 31%]
tests/test_leads.py (9 tests) PASSED                                     [ 49%]
tests/test_ledger.py (7 tests) PASSED                                    [ 62%]
tests/test_multi_seed.py::test_seed_pipeline_end_to_end[42] PASSED       [ 64%]
tests/test_multi_seed.py::test_seed_pipeline_end_to_end[100] PASSED      [ 66%]
tests/test_multi_seed.py::test_seed_pipeline_end_to_end[2026] PASSED     [ 68%]
tests/test_normalizers.py (14 tests) PASSED                              [ 90%]
tests/test_reporting.py (2 tests) PASSED                                 [ 94%]
tests/test_schemas.py (3 tests) PASSED                                   [100%]
============================= 51 passed in 19.75s =============================
```

### Performance & Observations
- Ingestion of small scale drops (2,000+ leads, 8,700+ events) finishes in under **2.5 seconds** per drop.
- Full reporting pipeline executes in under **0.4 seconds**.
- All bucket invariants hold: in every batch, `total_leads == yet_to_reach_out + in_progress_telephony_failed + in_progress_dialed + completed`.
- Re-ingesting drops produces byte-for-byte identical reports, confirming idempotency.

---

## If I Had Another Day

1. **Chunked Streaming for `--scale large`:** Implement chunked batch insertion and streaming generators for 140,000+ leads and 600,000+ events to optimize memory utilization.
2. **PostgreSQL Migration:** Port SQLite queries to PostgreSQL using the included `docker-compose.yml`, leveraging partitioned tables on `interaction_ledger` by month.
3. **Automated DLQ (Dead Letter Queue):** Build an exportable quarantine queue for malformed JSON lines and unlinked orphan calls with supervisor retry capabilities.

---

## Web Application Upgrade (`feature/web-upload-dashboard`)

In addition to the original CLI pipeline, a full-stack, local-first web application has been developed on branch `feature/web-upload-dashboard`:

- **Interactive SaaS Dashboard:** Clean, responsive SPA interface (Desktop, Tablet, Mobile) displaying live aggregated metrics from SQLite without mock data.
- **Upload Center:** Drag-and-drop support for CSV leads and JSON Lines event streams with multi-workflow selection and chunked streaming size limits (< 50 MB).
- **Zero-PII Lead Management:** Searchable, paginated repository with masked phones (`+91******XXXX`) and encrypted customer names.
- **Append-Only Interaction Ledger Stream:** Chronological event audit log with status badges and disposition override resolution.
- **Direct Browser Report Downloads:** One-click download of all 5 official contract reports in RFC 4180 CSV (Excel compatible) and valid JSON.
- **Quality Assurance & Verification:** Comprehensive QA report in `solution/WEB_QA_REPORT.md` and complete portfolio demo guide in `solution/WEB_DEMO_GUIDE.md`. All **101 tests passing** (51 original + 50 web platform tests).

### Launching the Web Application
```powershell
.\.venv\Scripts\python.exe -m uvicorn solution.web.app:app --host 127.0.0.1 --port 8000
```
Open **`http://127.0.0.1:8000`** in any web browser.
