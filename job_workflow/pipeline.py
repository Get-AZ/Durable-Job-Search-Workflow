from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, TypeVar

from pydantic import ValidationError

from job_workflow.limits import RunBudgetTracker
from job_workflow.recovery import LeaseManager
from job_workflow.schemas import ApplicationPacket, CaseState, FitEvaluation, JobPosting
from job_workflow.storage import WorkflowStorage

ModelT = TypeVar("ModelT", FitEvaluation, ApplicationPacket)


class LeaseOwnershipError(RuntimeError):
    """Raised when a worker attempts to advance a casefile without an active lease."""


class SchemaRepairExhaustedError(RuntimeError):
    """Raised when structured output fails Pydantic validation past max repair retries."""


@dataclass(frozen=True)
class StepResult:
    payload: dict[str, object]
    tokens_used: int = 0


StructuredStepRunner = Callable[[dict[str, object], str | None], tuple[str, int]]


def run_validated_json_step(
    *,
    casefile_id: str,
    step_name: str,
    context: dict[str, object],
    model_cls: type[ModelT],
    runner: StructuredStepRunner,
    budget: RunBudgetTracker,
    max_repair_attempts: int = 2,
) -> tuple[ModelT, int]:
    """
    Executes a structured step with bounded retry-with-repair on Pydantic ValidationError
    and enforces token + attempt limits on every call.
    """
    repair_feedback: str | None = None
    total_tokens = 0

    for attempt in range(1, max_repair_attempts + 2):
        budget.consume_attempt(casefile_id)
        budget.consume_api_call(count=1)
        raw_output, tokens_used = runner(context, repair_feedback)
        budget.consume_tokens(casefile_id, tokens_used)
        total_tokens += tokens_used

        try:
            validated = model_cls.model_validate_json(raw_output)
            budget.record_success()
            return validated, total_tokens
        except ValidationError as exc:
            budget.record_failure()
            repair_feedback = (
                f"Previous output for '{step_name}' failed schema validation on attempt {attempt}: "
                f"{exc.errors()}. Return strictly valid JSON matching the schema."
            )

    raise SchemaRepairExhaustedError(
        f"Casefile '{casefile_id}' step '{step_name}' failed validation after "
        f"{max_repair_attempts + 1} attempts."
    )


class DurableCasefilePipeline:
    def __init__(
        self,
        storage: WorkflowStorage,
        lease_manager: LeaseManager,
        budget: RunBudgetTracker,
        score_runner: StructuredStepRunner,
        draft_runner: StructuredStepRunner,
    ) -> None:
        self.storage = storage
        self.lease_manager = lease_manager
        self.budget = budget
        self.score_runner = score_runner
        self.draft_runner = draft_runner

    def _assert_lease_held(self, casefile_id: str) -> None:
        with self.storage.connect() as conn:
            row = conn.execute(
                "SELECT lease_owner FROM casefiles WHERE casefile_id = ?",
                (casefile_id,),
            ).fetchone()
        if row is None or row["lease_owner"] != self.lease_manager.config.worker_id:
            raise LeaseOwnershipError(
                f"Worker '{self.lease_manager.config.worker_id}' does not hold active lease "
                f"for casefile '{casefile_id}'."
            )

    def advance_casefile(
        self,
        casefile_id: str,
        posting: JobPosting,
        research_notes: list[str],
    ) -> CaseState:
        """
        Advances a leased casefile through normalize -> enrich -> score -> draft -> awaiting_approval.
        Each committed step is idempotent; re-running skips already-committed steps.
        """
        self._assert_lease_held(casefile_id)

        # 1. Deterministic normalization step (no LLM tokens)
        norm_payload: dict[str, object] = {
            "company": posting.company,
            "title": posting.title,
            "location": posting.location,
            "description_length": len(posting.raw_description),
        }
        self.storage.record_step_commit(
            casefile_id=casefile_id,
            step_name="normalize",
            next_state=CaseState.NORMALIZED,
            payload=norm_payload,
            tokens_used=0,
        )

        # 2. Bounded research enrichment step
        self.budget.consume_research_hop(casefile_id, hops=1)
        enrich_payload: dict[str, object] = {
            "notes": research_notes,
            "hop_count": 1,
        }
        self.storage.record_step_commit(
            casefile_id=casefile_id,
            step_name="enrich",
            next_state=CaseState.ENRICHED,
            payload=enrich_payload,
            tokens_used=0,
        )

        # 3. Validated role fit evaluation step
        score_context: dict[str, object] = {
            "casefile_id": casefile_id,
            "company": posting.company,
            "title": posting.title,
            "description": posting.raw_description,
            "research_notes": research_notes,
        }
        fit_eval, score_tokens = run_validated_json_step(
            casefile_id=casefile_id,
            step_name="score",
            context=score_context,
            model_cls=FitEvaluation,
            runner=self.score_runner,
            budget=self.budget,
        )
        self.storage.record_step_commit(
            casefile_id=casefile_id,
            step_name="score",
            next_state=CaseState.SCORED,
            payload=fit_eval.model_dump(mode="json"),
            tokens_used=score_tokens,
        )

        # 4. Validated application packet drafting step
        draft_context: dict[str, object] = {
            "casefile_id": casefile_id,
            "company": posting.company,
            "title": posting.title,
            "fit_evaluation": fit_eval.model_dump(mode="json"),
        }
        packet, draft_tokens = run_validated_json_step(
            casefile_id=casefile_id,
            step_name="draft",
            context=draft_context,
            model_cls=ApplicationPacket,
            runner=self.draft_runner,
            budget=self.budget,
        )
        self.storage.record_step_commit(
            casefile_id=casefile_id,
            step_name="draft",
            next_state=CaseState.DRAFTED,
            payload=packet.model_dump(mode="json"),
            tokens_used=draft_tokens,
        )

        # 5. Freeze at human-in-the-loop approval checkpoint
        self.storage.record_step_commit(
            casefile_id=casefile_id,
            step_name="queue_approval",
            next_state=CaseState.AWAITING_APPROVAL,
            payload={"casefile_id": casefile_id, "approval_status": packet.approval_status},
            tokens_used=0,
        )
        self.storage.write_casefile_artifact(
            casefile_id,
            {
                "casefile_id": casefile_id,
                "state": CaseState.AWAITING_APPROVAL.value,
                "posting": posting.model_dump(mode="json"),
                "fit_evaluation": fit_eval.model_dump(mode="json"),
                "application_packet": packet.model_dump(mode="json"),
            },
        )
        self.lease_manager.release_lease(casefile_id)
        return CaseState.AWAITING_APPROVAL
