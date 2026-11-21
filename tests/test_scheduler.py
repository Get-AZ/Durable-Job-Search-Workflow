import json
from pathlib import Path

from job_workflow.limits import BudgetPolicy, RunBudgetTracker
from job_workflow.pipeline import DurableCasefilePipeline
from job_workflow.recovery import LeaseConfig, LeaseManager
from job_workflow.schemas import CaseState, JobPosting
from job_workflow.scheduler import ScheduledWorkflowRunner
from job_workflow.storage import WorkflowStorage


def test_scheduled_run_deduplicates_queues_approval_and_writes_manifest(tmp_path: Path) -> None:
    store = WorkflowStorage(db_path=tmp_path / "state.db", artifacts_dir=tmp_path / "casefiles")
    lease_mgr = LeaseManager(store, LeaseConfig(worker_id="sched-1", lease_duration_seconds=120))
    budget = RunBudgetTracker(policy=BudgetPolicy())

    def fake_score(_ctx: dict[str, object], _fb: str | None) -> tuple[str, int]:
        return json.dumps(
            {
                "overall_score": 86.0,
                "recommendation": "STRONG_APPLY",
                "matched_skills": ["Python", "SQLite"],
                "missing_gaps": [],
                "evidence_citations": ["Built durable job-search workflow harness"],
                "rationale": "Strong match for reliability and structured pipeline engineering.",
            }
        ), 150

    def fake_draft(ctx: dict[str, object], _fb: str | None) -> tuple[str, int]:
        return json.dumps(
            {
                "casefile_id": str(ctx["casefile_id"]),
                "tailored_summary": "Reliability-focused engineer building deterministic agent pipelines.",
                "resume_bullets": [
                    "Engineered atomic SQLite WAL state persistence and idempotent step commits.",
                    "Implemented strict Pydantic validation with bounded repair loops.",
                ],
                "outreach_draft": "Hello team, I built a durable workflow engine in Python and would love to connect.",
                "approval_status": "PENDING",
            }
        ), 200

    pipeline = DurableCasefilePipeline(
        storage=store,
        lease_manager=lease_mgr,
        budget=budget,
        score_runner=fake_score,
        draft_runner=fake_draft,
    )
    runner = ScheduledWorkflowRunner(
        storage=store,
        lease_manager=lease_mgr,
        pipeline=pipeline,
        budget=budget,
        manifests_dir=tmp_path / "run_manifests",
        research_provider=lambda _job: ["Confirmed remote engineering team"],
    )

    jobs = [
        JobPosting(
            source="greenhouse",
            company="Orbital Systems",
            title="Workflow Systems Engineer",
            canonical_url="https://boards.greenhouse.io/orbital/jobs/101",
            raw_description="Design fault-tolerant workflows, schema validation, and scheduled runners.",
        ),
        JobPosting(
            source="greenhouse",
            company="orbital systems",
            title="workflow systems engineer",
            canonical_url="https://boards.greenhouse.io/orbital/jobs/101",
            raw_description="Design fault-tolerant workflows, schema validation, and scheduled runners.",
        ),
    ]

    manifest = runner.execute_run("run-2026-11-20-01", jobs)
    assert manifest.discovered_count == 1
    assert manifest.duplicates_skipped == 1
    assert manifest.approvals_queued == 1
    assert manifest.tokens_consumed == 350
    assert manifest.errors == []
    assert (tmp_path / "run_manifests" / "run-2026-11-20-01.json").exists()

    with store.connect() as conn:
        row = conn.execute("SELECT state FROM casefiles").fetchone()
        assert row is not None
        assert row["state"] == CaseState.AWAITING_APPROVAL.value
