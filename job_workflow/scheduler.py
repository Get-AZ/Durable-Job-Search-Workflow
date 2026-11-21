from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from job_workflow.limits import BudgetExceededError, CircuitOpenError, RunBudgetTracker
from job_workflow.pipeline import DurableCasefilePipeline
from job_workflow.recovery import LeaseManager
from job_workflow.schemas import CaseState, JobPosting, RunManifest
from job_workflow.storage import WorkflowStorage

ResearchProvider = Callable[[JobPosting], list[str]]


class RunInvariantError(AssertionError):
    """Raised when post-run health and consistency assertions fail."""


class ScheduledWorkflowRunner:
    def __init__(
        self,
        *,
        storage: WorkflowStorage,
        lease_manager: LeaseManager,
        pipeline: DurableCasefilePipeline,
        budget: RunBudgetTracker,
        manifests_dir: Path,
        research_provider: ResearchProvider,
    ) -> None:
        self.storage = storage
        self.lease_manager = lease_manager
        self.pipeline = pipeline
        self.budget = budget
        self.manifests_dir = manifests_dir
        self.research_provider = research_provider
        self.manifests_dir.mkdir(parents=True, exist_ok=True)

    def _write_manifest_artifact(self, manifest: RunManifest) -> Path:
        target = self.manifests_dir / f"{manifest.run_id}.json"
        fd, tmp_path = tempfile.mkstemp(
            dir=self.manifests_dir, prefix=f".{manifest.run_id}_", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(manifest.model_dump(mode="json"), handle, indent=2, sort_keys=True)
            os.replace(tmp_path, target)
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise
        return target

    def verify_post_run_invariants(self, manifest: RunManifest) -> None:
        """Verifies database state, active leases, and manifest counters after a run."""
        if manifest.finished_at is None:
            raise RunInvariantError("Manifest finished_at must be set before verification.")
        if manifest.tokens_consumed != self.budget.run_tokens_used:
            raise RunInvariantError(
                f"Manifest tokens ({manifest.tokens_consumed}) != tracker tokens ({self.budget.run_tokens_used})."
            )

        with self.storage.connect() as conn:
            dangling = conn.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM casefiles
                WHERE lease_owner IS NOT NULL
                  AND state NOT IN (?, ?, ?)
                """,
                (
                    CaseState.SUBMITTED.value,
                    CaseState.REJECTED.value,
                    CaseState.TERMINAL_FAILED.value,
                ),
            ).fetchone()
            if dangling is not None and int(dangling["cnt"]) > 0:
                raise RunInvariantError(
                    f"Found {dangling['cnt']} non-terminal casefile(s) still holding leases after run."
                )

    def execute_run(
        self,
        run_id: str,
        postings: list[JobPosting],
        now: datetime | None = None,
    ) -> RunManifest:
        started_at = now or datetime.now(timezone.utc)
        manifest = RunManifest(run_id=run_id, started_at=started_at)

        # 1. Reclaim stale leases from prior crashes
        self.lease_manager.reclaim_expired_leases(now=started_at)

        # 2. Ingest postings idempotently and process new/eligible casefiles
        for posting in postings:
            casefile_id, created = self.storage.ingest_job_idempotent(posting)
            if not created:
                manifest.duplicates_skipped += 1
                continue

            manifest.discovered_count += 1
            if not self.lease_manager.acquire_lease(casefile_id, now=started_at):
                manifest.errors.append(f"Could not acquire lease for casefile '{casefile_id}'.")
                continue

            try:
                notes = self.research_provider(posting)
                final_state = self.pipeline.advance_casefile(casefile_id, posting, notes)
                manifest.transitions_committed += 1
                if final_state == CaseState.AWAITING_APPROVAL:
                    manifest.approvals_queued += 1
            except (BudgetExceededError, CircuitOpenError) as exc:
                self.lease_manager.release_lease(casefile_id)
                manifest.errors.append(f"Budget/circuit halt on '{casefile_id}': {exc}")
                break
            except Exception as exc:  # noqa: BLE001
                self.lease_manager.release_lease(casefile_id)
                manifest.errors.append(f"Error processing '{casefile_id}': {exc}")

        manifest.tokens_consumed = self.budget.run_tokens_used
        manifest.finished_at = datetime.now(timezone.utc)

        # 3. Persist manifest and verify invariants
        self.storage.save_run_manifest(manifest)
        self._write_manifest_artifact(manifest)
        self.verify_post_run_invariants(manifest)
        return manifest
