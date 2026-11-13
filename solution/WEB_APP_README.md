# Interaction Ledger & Reporting Web Application
**Web-Based Data Upload, Processing, and Analytics Platform**

An enterprise-grade, local-first web application upgrading the Interaction Ledger & Reporting Pipeline into an interactive platform. It allows users to upload messy CSV leads and JSON Lines event streams directly from any modern web browser, processes data through transactional schema validation and encryption engines, and generates live business intelligence dashboards and contract-compliant audit reports.

---

## 1. Key Features

- **Upload Center:**
  - Drag-and-drop upload for Customer Leads (`.csv`) and Call Interaction Events (`.jsonl` / `.json`).
  - Supports individual uploads (`/api/upload/leads`, `/api/upload/events`) and atomic bundle uploads (`/api/upload/bundle`).
  - Strict 50 MB chunked streaming size limit enforcement to prevent memory exhaustion.
  - Multi-workflow selector (`motor_renewal` and `policy_feedback`).
  - Real-time processing progress with double-submit protection.
  - Immediate visual ingestion telemetry: accepted leads, rejected rows, duplicate rows merged, field warnings, processed events, malformed lines, dropped duplicates, and orphan events.

- **Dashboard Overview:**
  - Real-time query directly from SQLite (WAL mode) — **zero hardcoded demo metrics**.
  - KPI Cards: Total Leads, Total Calls (verified initiated), Connected Calls (with 4-decimal connectivity rate), Successful Leads, Total Stream Events, and Active Orphan Events.
  - Call Bucket Distribution adhering strictly to specification precedence:
    - *Completed* (Terminal disposition)
    - *In Progress Dialed* (Non-terminal initiated/connected)
    - *Telephony Failed* (All call attempts failed telephony)
    - *Yet to Reach Out* (Zero calls initiated)
  - Recent Daily Overview (IST) table with date-grouped volume and success rates.

- **Lead Management:**
  - Searchable, paginated table with debounced search across external reference and masked phone substring.
  - Workflow filter (`motor_renewal` vs `policy_feedback`) and call bucket filter.
  - **Zero Plaintext PII:** Plaintext customer names and raw phone numbers are never returned in network responses. Phones are masked as `+91******XXXX`.

- **Append-Only Interaction Ledger:**
  - Full audit trail of raw interaction events (`call.initiated`, `call.connected`, `disposition.set`, `disposition.override`, `call.telephony_failed`).
  - Live tracking of orphan reconciliation and disposition overrides.
  - Filter by event type and pagination controls.

- **Official Reports & Downloads:**
  - Real-time status cards tracking all 5 contract reports:
    1. `batch_summary.csv`
    2. `daily_overview.csv`
    3. `lead_report.csv`
    4. `rejections.csv`
    5. `data_quality.json`
  - Live metadata: record count, file size in KB, and ISO last-updated timestamps.
  - Traversal-safe, one-click browser downloads formatted in RFC 4180 CSV (Excel compatible) and valid JSON with no-cache headers.

- **Data Quality Audit:**
  - Visual rejection frequency breakdown by reason (`invalid_phone`, `missing_required:customer_name`, `invalid_date:policy_expiry`, etc.).
  - Stream telemetry audit counters for dropped duplicates, malformed event lines, unknown disposition codes, and non-critical field warnings.

---

## 2. Technology Stack

- **Backend Framework:** FastAPI (Python 3.11) with Uvicorn ASGI server
- **Database Engine:** SQLite 3 with Write-Ahead Logging (WAL) and `IMMEDIATE` transactions
- **Security & Cryptography:** Python `cryptography` library (AES-256-GCM authenticated encryption, HMAC-SHA256 blind indexing)
- **Frontend Architecture:** Modern Single-Page Application (SPA) using HTML5, Tailwind CSS, and Vanilla JavaScript
- **No Node.js / NPM Required:** Zero build steps, zero frontend dependencies to compile, completely self-contained

---

## 3. Quick Start & Installation

### Prerequisites
- Windows 10/11 (or Linux/macOS)
- Python 3.11+
- Google Chrome or any modern web browser

### Step 1: Clone and Navigate to Repository
```powershell
cd "c:\Users\Guest user\Desktop\SDE-Assignment-2026"
```

### Step 2: Set Up Python Virtual Environment
```powershell
# Create virtual environment if not already created
python -m venv .venv

# Activate virtual environment
.\.venv\Scripts\Activate.ps1
```

### Step 3: Install Required Dependencies
```powershell
pip install -r requirements.txt
```
*(Dependencies: `fastapi`, `uvicorn`, `pydantic`, `cryptography`, `python-multipart`, `httpx`, `pytest`)*

### Step 4: Configure Encryption Key (Optional)
The system uses `LEDGER_ENCRYPTION_KEY` and `LEDGER_HMAC_SALT` if set in environment, or automatically generates a secure persistent instance key.
```powershell
# Optional custom key:
$env:LEDGER_ENCRYPTION_KEY = "your-32-byte-base64-or-hex-key"
```

### Step 5: Launch Web Application
```powershell
.\.venv\Scripts\python.exe -m uvicorn solution.web.app:app --host 127.0.0.1 --port 8000
```

### Step 6: Access Dashboard
Open your web browser and navigate to:
**[http://127.0.0.1:8000](http://127.0.0.1:8000)**

---

## 4. API Endpoints Reference

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/` | Serves the Single-Page Application dashboard |
| `GET` | `/api/health` | Health check, DB connection status, leads & events counts |
| `GET` | `/api/dashboard/stats` | Aggregated KPIs, bucket distribution, daily activity, data quality |
| `GET` | `/api/dashboard/leads` | Searchable, paginated leads (`q`, `workflow_id`, `bucket`, `page`, `limit`) |
| `GET` | `/api/dashboard/ledger` | Paginated immutable events stream (`workflow_id`, `event_type`, `call_id`) |
| `POST` | `/api/upload/leads` | Ingests CSV leads with workflow validation and 50 MB streaming check |
| `POST` | `/api/upload/events` | Ingests JSONL events stream with orphan reconciliation |
| `POST` | `/api/upload/bundle` | Single-request coordinated upload of both leads CSV and events JSONL |
| `GET` | `/api/reports/status` | File presence, record counts, sizes, and timestamps for 5 reports |
| `GET` | `/api/reports/download/{filename}` | Whitelist-validated, traversal-safe download of official reports |
| `GET` | `/docs` | Interactive Swagger / OpenAPI documentation |

---

## 5. Security Architecture

1. **Zero-PII API Contract:**
   Customer names and raw phone numbers are never returned in network responses. Phone numbers are masked (`+91******XXXX`) before returning to the frontend.
2. **AES-256-GCM & HMAC Blind Indexing:**
   Customer names and phone numbers are encrypted with authenticated AES-256-GCM. Lookups are executed via deterministic HMAC-SHA256 blind indexes without decrypting rows.
3. **Path Traversal Protection:**
   Upload filenames are sanitized via `Path(name).name` with non-alphanumeric stripping. Report downloads are restricted strictly to `ALLOWED_REPORT_FILES`.
4. **SQL Injection Defense:**
   All queries utilize parameterized SQLite bindings (`?`). Zero dynamic SQL string concatenation.
5. **Streaming Upload Limit Enforcement:**
   Uploads are validated chunk-by-chunk in memory buffers (< 64 KB). Files exceeding 50 MB are terminated immediately and partial files deleted.

---

## 6. Testing & Quality Assurance

Run the complete test suite (101 tests):
```powershell
.\.venv\Scripts\pytest.exe
```

Run specific test categories:
```powershell
# Security and penetration tests
.\.venv\Scripts\pytest.exe tests/test_web_security_qa.py

# End-to-end integration tests
.\.venv\Scripts\pytest.exe tests/test_web_integration.py

# Original Assignment baseline tests
.\.venv\Scripts\pytest.exe tests/test_cli.py tests/test_leads.py tests/test_events.py tests/test_ledger.py
```
