from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from job_workflow.schemas import CaseState
from job_workflow.storage import WorkflowStorage


class ApprovalStateError(RuntimeError):
    """Raised when an approval decision is applied to a casefile not awaiting approval."""


class ApprovalGate:
    def __init__(self, storage: WorkflowStorage) -> None:
        self.storage = storage

    def list_pending_approvals(self) -> list[dict[str, object]]:
        with self.storage.connect() as conn:
            rows = conn.execute(
                """
                SELECT casefile_id, company, title, canonical_url, state, updated_at
                FROM casefiles
                WHERE state = ?
                ORDER BY updated_at ASC
                """,
                (CaseState.AWAITING_APPROVAL.value,),
            ).fetchall()
        return [dict(row) for row in rows]

    def decide(
        self,
        casefile_id: str,
        decision: Literal["APPROVED", "REJECTED"],
        reviewer_notes: str | None = None,
    ) -> CaseState:
        """
        Records an explicit human sign-off for a casefile in AWAITING_APPROVAL state.
        Transitions idempotently to APPROVED or REJECTED.
        """
        target_state = CaseState.APPROVED if decision == "APPROVED" else CaseState.REJECTED
        now = datetime.now(timezone.utc).isoformat()

        with self.storage.connect() as conn:
            row = conn.execute(
                "SELECT state FROM casefiles WHERE casefile_id = ?",
                (casefile_id,),
            ).fetchone()
            if row is None:
                raise ApprovalStateError(f"Casefile '{casefile_id}' does not exist.")

            current_state = str(row["state"])
            if current_state == target_state.value:
                return target_state
            if current_state != CaseState.AWAITING_APPROVAL.value:
                raise ApprovalStateError(
                    f"Cannot apply '{decision}' when casefile '{casefile_id}' is in state '{current_state}'."
                )

        committed = self.storage.record_step_commit(
            casefile_id=casefile_id,
            step_name=f"decision_{decision.lower()}",
            next_state=target_state,
            payload={
                "casefile_id": casefile_id,
                "decision": decision,
                "reviewer_notes": reviewer_notes,
                "decided_at": now,
            },
            tokens_used=0,
        )
        if not committed:
            raise ApprovalStateError(
                f"Decision '{decision}' for casefile '{casefile_id}' was already recorded."
            )
        return target_state
