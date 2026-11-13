# AI Usage Disclosure

AI tools are allowed. We want to know **how** you used them, because using them well is a
skill. Honest answers are never held against you. Discovering undisclosed use is.

## Tools Used
- Antigravity IDE Agent (powered by Google DeepMind's Gemini 3.8 Flash model).

---

## 1. Assignment A Core Implementation

### What I Used AI For:
- Scaffolding the initial project module structure inside `solution/` and test layout inside `tests/`.
- Generating comprehensive test fixtures for diverse date formats (`DD-Mon-YYYY`, `DD/MM/YYYY`, `DD-MM-YYYY`, `YYYY-MM-DD`), currency formats (`₹`, `Rs`, `Rs.`, `INR`), and phone edge cases.
- Drafting regular expression patterns for parsing header aliases, date formats, and currency strings.
- Drafting SQL queries with window functions (`ROW_NUMBER() OVER (...)`) for append-only effective disposition resolution.

### What I Wrote or Decided Myself:
- **Cryptographic PII Protection:** Decided to reject Base64 as pseudo-encryption and instead implemented proper authenticated symmetric encryption (AES-256-GCM with 96-bit nonces and 128-bit authentication tags) coupled with a deterministic HMAC-SHA256 blind index for indexing and matching phone numbers without decryption.
- **Data Model & Schema:** Designed SQLite tables (`leads`, `interaction_ledger`, `rejections`, `file_metrics`, `ingested_batches`) and unique constraints (`(workflow_id, phone_hash)` on leads, `(workflow_id, batch_id, row_number)` on rejections).
- **Strict Schema-Order Short-Circuiting:** Ensured validation iterates strictly over fields in the JSON schema's declared order, stopping at the first failure (`missing_required:*` or `invalid_phone`).
- **File-Level Idempotent Accounting (`file_metrics`):** Designed the per-file metric tracking system so that re-ingesting drops updates file metrics instead of incrementing in-memory counters, ensuring 100% idempotency.
- **Bidirectional Orphan Resolution:** Implemented logic for handling events that arrive before their leads, leads that arrive before events, and `disposition.override` events that arrive before their call's `call.initiated`.
- **4-Tier Call Bucketing Precedence:** Implemented the exact hierarchy (`yet_to_reach_out` $\rightarrow$ `completed` $\rightarrow$ `in_progress_telephony_failed` $\rightarrow$ `in_progress_dialed`).

### Where the AI Was Wrong or Unhelpful:
- **SQLite Transaction Auto-Commit:** During early integration of `cli.py`, the database context manager did not invoke `conn.commit()` upon completion, which caused SQLite to silently roll back DML operations when the connection closed. This resulted in `data_quality.json` returning zeros during initial verification. I diagnosed the uncommitted transaction issue, updated `get_db_connection` to commit on normal exit and rollback on exception, and added explicit `conn.commit()` calls at the end of each batch ingestion.
- **Off-by-One in Phone Masking Test:** An AI-generated unit test asserted that `+917718750036` should mask to `+91******5036`. However, the digits are `917718750036` (12 digits), where the last four digits are `0036`. Pytest caught the assertion failure, and I corrected the test.

---

## 2. Web Application Tasks 1–5 Hardening & Enhancements

During iterative hardening and enhancement on branch `feature/web-upload-dashboard`:

### Task 1 — DOM XSS Vulnerability Remediation:
- **AI Role:** Assisted in auditing all six views in `solution/web/static/index.html` to identify raw string interpolations in `innerHTML`.
- **Human Decision & Verification:** Enforced the implementation of a client-side `escapeHtml()` sanitizer. Verified that dynamic attributes and URLs use text assignments or strict attribute validation. Authoring browser-based regression tests (`tests/verify_xss_in_browser.py`) asserting zero script execution.

### Task 2 — Manual Disposition Outcome Override:
- **AI Role:** Drafted the modal HTML markup and backend route signature for `/api/dashboard/leads/{lead_id}/override`.
- **Human Decision & Verification:** Inspected the event ingestion pipeline to ensure overrides are ingested through existing validation logic rather than a parallel database insert. Enforced that overrides create append-only `disposition.override` events without modifying historical events, and verified precedence resolution across multiple overrides.

### Task 3 — Cryptographic Key Management Hardening:
- **AI Role:** Identified fallback development keys in `solution/config.py` and drafted key length checks.
- **Human Decision & Verification:** Mandated that production and staging environments fail fast via `CryptoConfigError` if secrets are absent or weak. Removed silent SHA-256 fallback hashing. Validated AES-256 keys as strictly 64 hexadecimal characters. Guaranteed complete redaction of keys from logs and health responses while preserving backward compatibility with local evaluation defaults.

### Task 4 — Dashboard Metrics & Data Quality Consistency:
- **AI Role:** Assisted in cross-referencing dashboard metric calculations with `solution/reporting.py`.
- **Human Decision & Verification:** Identified that the discrepancy between Dashboard "Total Leads" ($2,105$) and `data_quality.json` ($2,161$) was caused by ignoring 57 merged duplicate rows and conflating unique entities with raw input rows. Established the core reconciliation identity:
  $$\text{Total Input Rows} = \text{Unique Leads} + \text{Duplicates Merged} + \text{Rejected Rows}$$
  Added the dedicated reconciliation section to View 6 and renamed KPI Card 1 to "Unique Leads" with dynamic row-accounting subtitles.

### Task 5 — Upload Notification Accuracy & Transaction Boundaries:
- **AI Role:** Drafted status mapping and results card styling classes for varied upload results.
- **Human Decision & Verification:** Architected two-phase transaction boundaries: committing SQLite ingestion transactions *before* attempting report generation. Prevented report generation failures from rolling back committed ledger records. Enforced that 100% rejected uploads display a red rejection state (`❌`) and never claim green success. Created comprehensive test suites for all 8 scenarios (`tests/test_web_upload_notifications.py`, `tests/verify_upload_notifications_in_browser.py`).

---

## 3. Verification Methodology

Every line of AI-assisted code was subjected to rigorous validation:
1. **Automated Regression Suite:** 148 automated tests executed via `pytest -v`, achieving 100% pass rate.
2. **Playwright Browser Testing:** Live Chrome browser automation scripts verifying responsive viewports, modal interactions, file downloads, XSS payload neutralization, and toast notifications.
3. **Multi-Seed Determinism:** Validated across seeds 42, 100, and 2026 ensuring identical report outputs.
4. **Git Isolation:** Confirmed all work was performed strictly on `feature/web-upload-dashboard` while `main` remains untouched and identical to the original Assignment A baseline.
