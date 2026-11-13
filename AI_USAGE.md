# AI Usage Disclosure

AI tools are allowed. We want to know **how** you used them, because using them well is a
skill. Honest answers are never held against you. Discovering undisclosed use is.

## Tools used
- Antigravity IDE Agent (powered by Google DeepMind's Gemini 3.8 Flash model).

## What I used them for
- Scaffolding the initial project module structure inside `solution/` and test layout inside `tests/`.
- Generating comprehensive test cases for diverse date formats (`DD-Mon-YYYY`, `DD/MM/YYYY`, `DD-MM-YYYY`, `YYYY-MM-DD`), currency formats (`₹`, `Rs`, `Rs.`, `INR`), and phone edge cases.
- Drafting regular expression patterns for parsing header aliases, date formats, and currency strings.
- Drafting SQL queries with window functions (`ROW_NUMBER() OVER (...)`) for append-only effective disposition resolution.

## What I wrote or decided myself
- **Cryptographic PII Protection:** Decided to reject Base64 as pseudo-encryption and instead implemented proper authenticated symmetric encryption (AES-256-GCM with 96-bit nonces and 128-bit authentication tags) coupled with a deterministic HMAC-SHA256 blind index for indexing and matching phone numbers without decryption.
- **Data Model & Schema:** Designed SQLite tables (`leads`, `interaction_ledger`, `rejections`, `file_metrics`, `ingested_batches`) and unique constraints (`(workflow_id, phone_hash)` on leads, `(workflow_id, batch_id, row_number)` on rejections).
- **Strict Schema-Order Short-Circuiting:** Ensured validation iterates strictly over fields in the JSON schema's declared order, stopping at the first failure (`missing_required:*` or `invalid_phone`).
- **File-Level Idempotent Accounting (`file_metrics`):** Designed the per-file metric tracking system so that re-ingesting drops updates file metrics instead of incrementing in-memory counters, ensuring 100% idempotency.
- **Bidirectional Orphan Resolution:** Implemented logic for handling events that arrive before their leads, leads that arrive before events, and `disposition.override` events that arrive before their call's `call.initiated`.
- **4-Tier Call Bucketing Precedence:** Implemented the exact hierarchy (`yet_to_reach_out` $\rightarrow$ `completed` $\rightarrow$ `in_progress_telephony_failed` $\rightarrow$ `in_progress_dialed`).

## Where the AI was wrong or unhelpful
- **SQLite Transaction Auto-Commit:** During early integration of `cli.py`, the database context manager did not invoke `conn.commit()` upon completion, which caused SQLite to silently roll back DML operations when the connection closed. This resulted in `data_quality.json` returning zeros during initial verification. I diagnosed the uncommitted transaction issue, updated `get_db_connection` to commit on normal exit and rollback on exception, and added explicit `conn.commit()` calls at the end of each batch ingestion.
- **Off-by-One in Phone Masking Test:** An AI-generated unit test asserted that `+917718750036` should mask to `+91******5036`. However, the digits are `917718750036` (12 digits), where the last four digits are `0036`. Pytest caught the assertion failure, and I corrected the test.

## How I verified AI-generated code
- **Line-by-Line Code Review:** Manually inspected every module in `solution/` and `tests/` to verify logic, typing, error handling, and specification compliance.
- **Automated Test Suite:** Built and ran 51 automated unit and integration tests with `pytest`, achieving 100% pass rate.
- **Multi-Seed Verification:** Executed the official synthetic data generator across three distinct seeds (**42**, **100**, and **2026**) with fresh databases, validating all invariants (lead bucket sums, daily call counts, connectivity rates, PII masking, orphan event counts).
- **Idempotency Stress Testing:** Ingested the same drops repeatedly, asserting that database row counts remained unchanged and generated reports were byte-for-byte identical.

---

## Web Application Upgrade (`feature/web-upload-dashboard`)

During the upgrade of the application into a web platform:
- **Tools Used:** Antigravity IDE Agent.
- **What I used them for:**
  - Scaffolding the FastAPI backend application structure (`solution/web/app.py`, `solution/web/routes/upload.py`, `solution/web/routes/dashboard.py`, `solution/web/routes/reports.py`).
  - Drafting responsive Tailwind CSS and modern SaaS SPA frontend components (`solution/web/static/index.html`).
  - Authoring Playwright browser automation scripts (`tests/test_performance_and_responsive.py`, `tests/verify_browser.py`) for multi-viewport responsive testing and automated file download verification.
  - Designing security penetration test fixtures for SQL injection, path traversal, and streaming chunked size enforcement (`tests/test_web_security_qa.py`).
- **Where the AI was caught or adjusted:**
  - *Empty (0-byte) File Uploads:* Initially, 0-byte files returned HTTP 200 with 0 processed leads instead of failing validation. I caught this during QA, added explicit `file_size == 0` validation checks in `upload.py`, returning HTTP 400 Bad Request.
  - *Missing CSV Column Headers:* CSVs with arbitrary column headers were being parsed row-by-row rather than failing upfront. I implemented `_validate_lead_csv_headers()` in `upload.py` to cross-reference header aliases against workflow schemas and reject invalid files immediately.
  - *Debounce Event Binding:* An initial implementation recreated debounce timers on each keystroke in `onkeyup`. I refactored the search input to use a shared debounced handler on `oninput="onSearchLeads()"`, properly resetting pagination to page 1.
  - *Mobile Drawer UX:* Auto-closed the off-canvas navigation sidebar when links were clicked on small viewports (< 768px).
- **Verification:** Ran all 101 automated tests (51 original CLI + 50 web platform tests), empirical performance benchmarking, and multi-viewport browser testing in Google Chrome.
