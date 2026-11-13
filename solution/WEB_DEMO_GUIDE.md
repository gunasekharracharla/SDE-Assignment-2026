# Interaction Ledger & Reporting Platform – Live Demo & Portfolio Walkthrough Guide

**Document Purpose:** Complete walkthrough guide for evaluating, demonstrating, and testing the Interaction Ledger & Reporting Web Application.  
**Platform Version:** 1.0.0 (Web Edition)  
**Target Environment:** Localhost (`http://127.0.0.1:8000`)  
**Git Branch:** `feature/web-upload-dashboard`  

---

## 1. Project Overview

The **Interaction Ledger & Reporting Platform** is a secure, local web application designed to automate the ingestion, validation, encryption, deduplication, and reporting of customer leads and call interaction event streams for enterprise campaigns (such as Motor Insurance Renewal and Policy Feedback).

Originally built as a command-line ingestion pipeline (Assignment A), this project has been upgraded into a modern, responsive Single-Page Application (SPA) dashboard backed by FastAPI and SQLite.

### Core Business Capabilities
1. **Multi-Workflow Ingestion:** Ingests messy, real-world customer leads CSVs and asynchronous call streams in JSON Lines (`.jsonl`).
2. **Schema Normalization & Order Preservation:** Enforces field types, dates, decimal premium parsing, and schema-order normalization.
3. **Enterprise Security & PII Protection:** Encrypts customer names and phone numbers with authenticated **AES-256-GCM** and uses **HMAC-SHA256 blind indexing** for zero-knowledge phone lookups. Plaintext PII is never exposed to the frontend or API.
4. **Append-Only Interaction Ledger:** Immutable event ledger maintaining primary-key uniqueness, handling call disposition overrides (`disposition.override`), and auto-reconciling orphan events.
5. **Business Intelligence & Call Bucketing:** Categorizes each lead into one of four mutually exclusive call buckets using strict priority rules:
   - `completed` (Terminal disposition)
   - `in_progress_dialed` (Non-terminal initiated/connected)
   - `in_progress_telephony_failed` (All attempts telephony failed)
   - `yet_to_reach_out` (Zero calls initiated)
6. **Contract-Compliant Reporting:** Generates the exact 5 required reports with zero-index CSV formatting and RFC 4180 compliance.

---

## 2. Architecture & Technology Stack

```
┌────────────────────────────────────────────────────────────────────────┐
│                   RESPONSIVE SPA FRONTEND (BROWSER)                    │
│   Tailwind CSS  •  Vanilla ES6 JS  •  Fetch API  •  Zero Build Tool    │
│   (Overview  |  Upload Center  |  Leads  |  Ledger  |  Reports  |  DQ) │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ HTTP REST / Multipart Streaming
┌───────────────────────────────────▼────────────────────────────────────┐
│                         FASTAPI BACKEND SERVER                         │
│   • CORS Guard (Localhost)         • Chunked Streaming (< 50 MB)       │
│   • Input Validation & Whitelist   • Traversal & SQLi Protection       │
│   • Zero-PII Response Serializer   • Exception Handling & Logging      │
└─────────┬─────────────────────────┬─────────────────────────┬──────────┘
          │                         │                         │
┌─────────▼──────────────┐┌─────────▼──────────────┐┌─────────▼──────────┐
│   INGESTION PIPELINE   ││   SECURITY & CRYPTO    ││  REPORTING ENGINE  │
│ • Schema Validator     ││ • AES-256-GCM          ││ • batch_summary    │
│ • Header Alias Map     ││ • HMAC-SHA256 Blind Idx││ • daily_overview   │
│ • Rejection Logger     ││ • Phone Masking        ││ • lead_report      │
│ • Orphan Reconciler    ││ • SHA-256 Checksums    ││ • rejections       │
│ • Disposition Override ││ • Ephemeral / Stored   ││ • data_quality     │
└─────────┬──────────────┘└─────────┬──────────────┘└─────────┬──────────┘
          │                         │                         │
┌─────────▼─────────────────────────▼─────────────────────────▼──────────┐
│                      SQLITE 3 DATABASE (WAL MODE)                      │
│   • leads (encrypted)             • ingested_batches                   │
│   • interaction_ledger            • file_metrics                       │
│   • rejections                    • schema_migrations                  │
└────────────────────────────────────────────────────────────────────────┘
```

- **Backend:** Python 3.11, FastAPI 0.115+, Uvicorn 0.29+
- **Database:** SQLite 3 with Write-Ahead Logging (`PRAGMA journal_mode=WAL`)
- **Cryptography:** Python `cryptography` (Fernet / AES-256-GCM, HMAC-SHA256)
- **Frontend:** HTML5, Tailwind CSS, Vanilla JavaScript (No Node.js, No npm, Zero compile step)
- **QA & Automation:** `pytest`, `httpx`, `playwright`

---

## 3. Installation & Local Startup

### Step 1: Open Windows PowerShell and Navigate to Project
```powershell
cd "c:\Users\Guest user\Desktop\SDE-Assignment-2026"
```

### Step 2: Verify or Create Virtual Environment
```powershell
# Create venv if needed
python -m venv .venv

# Activate venv
.\.venv\Scripts\Activate.ps1
```

### Step 3: Install Dependencies
```powershell
pip install -r requirements.txt
```

### Step 4: Start Backend Application Server
```powershell
.\.venv\Scripts\python.exe -m uvicorn solution.web.app:app --host 127.0.0.1 --port 8000 --reload
```

Output:
```text
INFO:     Started server process [11880]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
```

### Step 5: Open Application in Web Browser
Open Google Chrome, Microsoft Edge, or Firefox and go to:
**[http://127.0.0.1:8000](http://127.0.0.1:8000)**

---

## 4. Live Demonstration Walkthrough

### Walkthrough 1: Exploring the Overview Dashboard
1. On initial load, the **Dashboard Overview** displays live aggregated metrics queried directly from the SQLite database.
2. Observe the KPI cards:
   - **Total Leads:** 2,000+ (showing accepted / rejected counts)
   - **Total Calls:** 2,500+ verified initiated calls
   - **Connected Calls:** Total connected calls with exact 4-decimal connectivity rate (e.g. `57.9%`)
   - **Successful Leads:** Distinct leads reaching a terminal success disposition
   - **Total Stream Events:** Events processed in the append-only ledger
   - **Active Orphans:** Unmatched events awaiting lead ingestion
3. Review the **Leads by Call Bucket** progress bars showing distribution across `completed`, `in_progress_dialed`, `in_progress_telephony_failed`, and `yet_to_reach_out`.
4. Review the **Recent Daily Overview** table displaying call volume, connectivity rates, and success counts grouped by IST calendar dates.

---

### Walkthrough 2: Uploading Lead CSV Files
1. Click **Upload Center** in the sidebar (or navigate to `http://127.0.0.1:8000/#upload`).
2. Select your target workflow:
   - **Motor Renewal** (`motor_renewal`)
   - **Policy Feedback** (`policy_feedback`)
3. Under **1. Customer Leads (.csv)**, drag and drop a valid lead CSV file (or click to browse).
   - *Example test file:* `assignment-a-data/motor_renewal/batch_001.csv`
4. The file preview appears with the file name and size in KB.
5. Click **Upload & Process Data**.
6. The submit button is disabled with a spinner (*"Processing Ingestion Pipeline..."*) to prevent duplicate submissions.
7. Upon completion:
   - A green toast appears: *"Data uploaded and processed successfully! Reports regenerated."*
   - The **Ingestion Results Summary** card renders total lead rows, accepted leads, rejected leads, duplicate rows merged, and field warnings.
   - If invalid rows exist, the rejection reasons list displays row numbers and exact rejection reasons (e.g., `invalid_phone`, `missing_required:customer_name`).
8. The dashboard metrics update automatically.

---

### Walkthrough 3: Uploading JSONL Interaction Event Streams
1. In the **Upload Center**, keep or switch the workflow selector.
2. Under **2. Call Events (.jsonl / .json)**, drag and drop an events file.
   - *Example test file:* `assignment-a-data/motor_renewal/events.jsonl`
3. Click **Upload & Process Data**.
4. The system ingests the events into the append-only ledger:
   - Duplicates with identical `event_id` are automatically dropped.
   - Unknown disposition codes are isolated without breaking the stream.
   - Previously orphan events are auto-reconciled against newly available leads.
5. Ingestion telemetry shows processed events, dropped duplicate events, and remaining active orphans.

---

### Walkthrough 4: Combined Bundle Upload (CSV + JSONL)
1. Select the **Motor Renewal** workflow.
2. Drag and drop both a leads `.csv` file into Dropzone 1 AND an events `.jsonl` file into Dropzone 2.
3. Click **Upload & Process Data**.
4. The single atomic request (`POST /api/upload/bundle`) validates both files, executes lead ingestion first, runs event ingestion second, and finishes with orphan event reconciliation and full report regeneration.
5. The results card presents both lead metrics and event metrics in a unified telemetry view.

---

### Walkthrough 5: Lead Management & Zero-PII Search
1. Click **Lead Management** in the sidebar.
2. Notice the **Masked Phone** column: all customer phone numbers are masked as `+91******XXXX`. Customer names are encrypted and never transmitted across the network.
3. **Test Search:** Type a policy reference (e.g. `MOT/2026/100000`) or phone suffix (e.g. `1819`) into the search bar. The debounced filter updates the table in real time.
4. **Test Workflow Filter:** Select `Motor Renewal` or `Policy Feedback` to filter by campaign.
5. **Test Call Bucket Filter:** Select `Completed`, `In Progress Dialed`, `Telephony Failed`, or `Yet to Reach Out` to filter leads by current state.
6. Use the **Previous** and **Next** buttons to navigate through paginated pages.

---

### Walkthrough 6: Interaction Ledger Audit Stream
1. Click **Interaction Ledger** in the sidebar.
2. View the chronological stream of events with IST occurred timestamps, call IDs, masked phones, and status badges (`Linked` or `Orphan`).
3. Filter by event type using the dropdown (`call.initiated`, `call.connected`, `disposition.set`, `disposition.override`, `call.telephony_failed`).

---

### Walkthrough 7: Downloading Official Reports
1. Click **Official Reports** in the sidebar.
2. View the status cards for all five contract reports:
   - `batch_summary.csv`
   - `daily_overview.csv`
   - `lead_report.csv`
   - `rejections.csv`
   - `data_quality.json`
3. Each card displays the record count, size in KB, and exact last updated timestamp.
4. Click **Download File** on any report:
   - Downloads cleanly directly through the browser.
   - Verified 100% compatible with Microsoft Excel (RFC 4180 CSV, UTF-8 charset).
   - Valid JSON formatting for `data_quality.json`.

---

### Walkthrough 8: Data Quality Audit
1. Click **Data Quality Audit** in the sidebar.
2. Review the **Rejection Breakdown by Reason** with visual percentage bars.
3. Review the **Stream Telemetry & Event Integrity** card tracking malformed lines, dropped duplicates, unknown disposition codes, and active orphans.

---

## 5. Running the Test Suites

### Run All 101 Automated Tests
```powershell
.\.venv\Scripts\pytest.exe
```

### Run Specialized Test Suites
```powershell
# Security, SQLi, and Traversal Penetration Tests
.\.venv\Scripts\pytest.exe tests/test_web_security_qa.py

# End-to-End Pipeline Integration Tests
.\.venv\Scripts\pytest.exe tests/test_web_integration.py

# Report Downloads and Header Verification Tests
.\.venv\Scripts\pytest.exe tests/test_web_reports.py

# Dashboard Search, Pagination, and Filtering Tests
.\.venv\Scripts\pytest.exe tests/test_web_dashboard.py

# Original Baseline Assignment Tests (All 51 Must Pass)
.\.venv\Scripts\pytest.exe tests/test_cli.py tests/test_crypto.py tests/test_db.py tests/test_events.py tests/test_leads.py tests/test_ledger.py tests/test_multi_seed.py tests/test_normalizers.py tests/test_reporting.py tests/test_schemas.py
```

### Run Performance Benchmarking & Browser Automation
```powershell
python tests/test_performance_and_responsive.py
```

---

## 6. Visual Artifacts & Verified Screenshots

All screenshots were captured from actual Google Chrome browser rendering sessions running on `http://127.0.0.1:8000`:

| View | Screenshot File |
|---|---|
| **Dashboard Overview** | `screen_dashboard.png` |
| **Upload Center (Results)** | `screen_upload_results.png` |
| **Official Reports** | `screen_reports.png` |
| **Lead Management** | `screen_leads.png` |
| **Debounced Search** | `screen_leads_searched.png` |
| **Interaction Ledger** | `screen_ledger.png` |
| **Filtered Ledger** | `screen_ledger_filtered.png` |
| **Data Quality Audit** | `screen_quality.png` |
| **Tablet Viewport** | `screen_tablet_overview.png` |
| **Mobile Viewport** | `screen_mobile_overview.png` |
| **Mobile Sidebar Drawer** | `screen_mobile_sidebar.png` |

---

## 7. Handover Checklist & Verification

- [x] Application launches with `.\.venv\Scripts\python.exe -m uvicorn solution.web.app:app --host 127.0.0.1 --port 8000`
- [x] All 51 original CLI/pipeline assignment tests continue passing.
- [x] All 50 new web platform and security tests pass (101 total).
- [x] Zero customer PII or encryption keys leaked in APIs or logs.
- [x] 50 MB upload limits enforced while streaming.
- [x] All five contract reports download cleanly in browser and open in Excel.
- [x] Git branch `feature/web-upload-dashboard` kept isolated from `main`.
- [x] Zero unapproved remote pushes.
