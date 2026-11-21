import json
from pathlib import Path
import pytest

from job_workflow.approval import ApprovalGate, ApprovalStateError
from job_workflow.limits import BudgetPolicy, RunBudgetTracker
from job_workflow.pipeline import DurableCasefilePipeline
from job_workflow.recovery import LeaseConfig, LeaseManager
from job_workflow.schemas import CaseState, JobPosting
from job_workflow.storage import WorkflowStorage


def test_pipeline_halts_at_awaiting_approval_and_allows_explicit_signoff(tmp_path: Path) -> None:
    store = WorkflowStorage(db_path=tmp_path / "state.db", artifacts_dir=tmp_path / "casefiles")
    job = JobPosting(
        source="greenhouse",
        company="Northstar AI",
        title="Applied AI Engineer",
        canonical_url="https://boards.greenhouse.io/northstar/jobs/77",
        raw_description="Build production LLM pipelines, evaluation harnesses, and workflow guardrails.",
    )
    cid, _ = store.ingest_job_idempotent(job)

    lease_mgr = LeaseManager(store, LeaseConfig(worker_id="worker-1", lease_duration_seconds=120))
    assert lease_mgr.acquire_lease(cid) is True

    def fake_score(_ctx: dict[str, object], _fb: str | None) -> tuple[str, int]:
        return json.dumps(
            {
                "overall_score": 91.0,
                "recommendation": "STRONG_APPLY",
                "matched_skills": ["Python", "Pydantic"],
                "missing_gaps": [],
                "evidence_citations": ["Implemented typed contracts and test harnesses"],
                "rationale": "Matches core engineering patterns for durable AI systems.",
            }
        ), 200

    def fake_draft(_ctx: dict[str, object], _fb: str | None) -> tuple[str, int]:
        return json.dumps(
            {
                "casefile_id": cid,
                "tailored_summary": "Backend and AI systems engineer focused on deterministic reliability and validation.",
                "resume_bullets": [
                    "Designed idempotent SQLite WAL persistence and crash-recovery leases.",
                    "Enforced Pydantic schema validation with bounded repair loops.",
                ],
                "outreach_draft": "Hi team, I built durable workflow harnesses in Python and would love to connect on the role.",
                "approval_status": "PENDING",
            }
        ), 250

    budget = RunBudgetTracker(policy=BudgetPolicy())
    pipeline = DurableCasefilePipeline(
        storage=store,
        lease_manager=lease_mgr,
        budget=budget,
        score_runner=fake_score,
        draft_runner=fake_draft,
    )

    state = pipeline.advance_casefile(cid, job, research_notes=["Verified role requirements"])
    assert state == CaseState.AWAITING_APPROVAL

    gate = ApprovalGate(store)
    pending = gate.list_pending_approvals()
    assert len(pending) == 1
    assert pending[0]["casefile_id"] == cid

    decided = gate.decide(cid, "APPROVED", reviewer_notes="Looks solid, ready for submission.")
    assert decided == CaseState.APPROVED

    # Cannot approve again after decision is already finalized
    with pytest.raises(ApprovalStateError):
        gate.decide(cid, "REJECTED")
