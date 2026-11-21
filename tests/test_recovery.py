from datetime import datetime, timedelta, timezone
from pathlib import Path

from job_workflow.recovery import LeaseConfig, LeaseManager
from job_workflow.schemas import JobPosting
from job_workflow.storage import WorkflowStorage


def test_expired_lease_is_reclaimed_and_reacquired(tmp_path: Path) -> None:
    store = WorkflowStorage(db_path=tmp_path / "state.db", artifacts_dir=tmp_path / "casefiles")
    job = JobPosting(
        source="greenhouse",
        company="Acme AI",
        title="Agent Reliability Engineer",
        canonical_url="https://boards.greenhouse.io/acme/jobs/555",
        raw_description="Build durable workflows, state machines, and crash recovery harnesses.",
    )
    cid, _ = store.ingest_job_idempotent(job)

    t0 = datetime(2026, 11, 20, 10, 0, 0, tzinfo=timezone.utc)
    worker_a = LeaseManager(store, LeaseConfig(worker_id="worker-a", lease_duration_seconds=60))
    worker_b = LeaseManager(store, LeaseConfig(worker_id="worker-b", lease_duration_seconds=60))

    assert worker_a.acquire_lease(cid, now=t0) is True
    # Active lease prevents another worker from taking over early
    assert worker_b.acquire_lease(cid, now=t0 + timedelta(seconds=20)) is False

    # Simulate crash past lease expiry
    after_expiry = t0 + timedelta(seconds=65)
    reclaimed = worker_b.reclaim_expired_leases(now=after_expiry)
    assert reclaimed == [cid]
    assert worker_b.acquire_lease(cid, now=after_expiry) is True
