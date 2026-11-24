# Durable Job-Search Workflow

![Durable Job-Search Workflow — persistent state, crash recovery, and human approval](https://getamesay-ml-cloud-ai.getgad-uss.chatgpt.site/assets/durable-job-search-workflow.png)

Deterministic job-search ingestion, evaluation, and application-packet pipeline built with SQLite WAL persistence, idempotent step execution, lease-based crash recovery, strict Pydantic schema validation, and human-in-the-loop approval gates.

## Core Architecture

- **Persistent State Machine (`job_workflow/storage.py`, `job_workflow/pipeline.py`):** Tracks every posting as a durable casefile across `DISCOVERED -> ENRICHED -> SCORED -> AWAITING_APPROVAL -> APPROVED | REJECTED` in SQLIte (`data/state.db`) paired with JSON casefile artifacts (`data/casefiles/<id>.json`).
- **Idempotency & Crash Recovery (`job_workflow/recovery.py`):** Deterministic casefile IDs prevent duplicate ingestion across runs, while time-bounded worker leases reclaim interrupted jobs safely.
- **Validation & Budget Guardrails (`job_workflow/schemas.py`, `job_workflow/limits.py`):** Enforces Pydantic output contracts with bounded repair loops alongside per-run and per-casefile token/API call budgets.
- **Board Connectors (`job_workflow/sources.py`):** Normalizes postings from Greenhouse and Lever job boards into unified `JobPosting` records.
- **Human-in-the-Loop CLI (`job_workflow/cli.py`):** Inspect pending casefiles, approve or reject packets, and export approved packets as JSON or Markdown digests.

## Quickstart

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
```

## CLI Usage

```powershell
# Ingest and process a batch from configured Greenhouse/Lever sources
python -m job_workflow.cli --data-dir data run-sources --sources-file sources.json --run-id batch-01

# List casefiles awaiting human review
python -m job_workflow.cli --data-dir data pending

# Inspect a specific casefile
python -m job_workflow.cli --data-dir data inspect --casefile-id <casefile_id>

# Approve and export ready-to-submit packets
python -m job_workflow.cli --data-dir data approve --casefile-id <casefile_id> --notes "Strong systems fit"
python -m job_workflow.cli --data-dir data export-approved --format md --output data/approved_digest.md
python -m job_workflow.cli --data-dir data export-approved --format json --output data/approved_packets.json
```

## Verification

```powershell
mypy job_workflow
pytest
```
