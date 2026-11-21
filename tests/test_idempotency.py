from pathlib import Path
from job_workflow.schemas import CaseState, JobPosting
from job_workflow.storage import WorkflowStorage


def test_duplicate_job_ingestion_is_idempotent(tmp_path: Path) -> None:
    store = WorkflowStorage(db_path=tmp_path / "state.db", artifacts_dir=tmp_path / "casefiles")

    job_first = JobPosting(
        source="greenhouse",
        company="  Acme   AI  ",
        title="Senior ML Security Engineer",
        canonical_url="https://boards.greenhouse.io/acme/jobs/12345",
        raw_description="Build deterministic guardrails and agent workflows in Python.",
    )
    # Same job discovered again with different whitespace casing
    job_duplicate = JobPosting(
        source="greenhouse",
        company="acme ai",
        title="senior ml security engineer",
        canonical_url="https://boards.greenhouse.io/acme/jobs/12345",
        raw_description="Build deterministic guardrails and agent workflows in Python.",
    )

    id_1, created_1 = store.ingest_job_idempotent(job_first)
    id_2, created_2 = store.ingest_job_idempotent(job_duplicate)

    assert id_1 == id_2
    assert created_1 is True
    assert created_2 is False
    assert (tmp_path / "casefiles" / f"{id_1}.json").exists()


def test_step_commit_is_duplicate_safe(tmp_path: Path) -> None:
    store = WorkflowStorage(db_path=tmp_path / "state.db", artifacts_dir=tmp_path / "casefiles")
    job = JobPosting(
        source="lever",
        company="Beta Corp",
        title="AI Platform Engineer",
        canonical_url="https://jobs.lever.co/beta/9876",
        raw_description="Design durable agent orchestration pipelines with strict state checks.",
    )
    cid, _ = store.ingest_job_idempotent(job)

    first_commit = store.record_step_commit(cid, "normalize", CaseState.NORMALIZED, {"clean": True}, tokens_used=120)
    replay_commit = store.record_step_commit(cid, "normalize", CaseState.NORMALIZED, {"clean": True}, tokens_used=120)

    assert first_commit is True
    assert replay_commit is False