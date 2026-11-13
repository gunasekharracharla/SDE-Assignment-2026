# Solution: Interaction Ledger & Reporting Pipeline (Assignment A)

This package implements the official data ingestion, append-only interaction ledger, and reporting pipeline for Agentic Universe.

## Package Architecture

- `solution/config.py`: Environment configuration, paths, timezone definitions (`IST`, `UTC`), and cryptographic keys/salts.
- `solution/crypto.py`: Authenticated AES-256-GCM encryption/decryption for PII protection at rest, HMAC-SHA256 blind indexing, and phone masking.
- `solution/db.py`: SQLite connection factory, schema creation DDL, table constraints, and transaction management.
- `solution/schemas.py`: Schema loaders for `motor_renewal.json`, `policy_feedback.json`, and `dispositions.csv`; header normalization and alias mapping.
- `solution/normalizers.py`: Pure normalizers for Indian mobile phones, 4 accepted date formats (day-first), currency/decimal values, case-insensitive enums, and timestamps.
- `solution/leads.py`: Lead CSV parser, schema-order validation, rejection logger (zero PII), duplicate lead merger (last-write-wins), schema defaults, and batch idempotency.
- `solution/events.py`: JSON Lines event stream parser, malformed line isolation, event deduplication, and append-only ledger writer.
- `solution/ledger.py`: Effective disposition resolution (SQL window queries), valid call counting (`call.initiated` required), 4-tier call bucket derivation, and bidirectional orphan reconciliation.
- `solution/reporting.py`: Generator producing all five exact report files matching the official output contract.
- `solution/cli.py`: Command-line interface for `ingest <drop_path>` and `report --out <out_dir>`.

## CLI Usage

```bash
# Ingest drop 1
python -m solution.cli ingest data/drop_1

# Ingest drop 2
python -m solution.cli ingest data/drop_2

# Generate reports
python -m solution.cli report --out reports/
```

## Running Tests

```bash
pytest -v
```
