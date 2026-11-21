from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from job_workflow.schemas import CaseState
from job_workflow.storage import WorkflowStorage


@dataclass(frozen=True)
class LeaseConfig:
    worker_id: str
    lease_duration_seconds: int = 120


class LeaseManager:
    def __init__(self, storage: WorkflowStorage, config: LeaseConfig) -> None:
        self.storage = storage
        self.config = config

    def acquire_lease(
        self,
        casefile_id: str,
        now: datetime | None = None,
    ) -> bool:
        """
        Acquires an exclusive time-bounded lease if the casefile is unleased
        or its previous lease has expired. Increments attempt_count on acquisition.
        """
        current = now or datetime.now(timezone.utc)
        expires_at = (current + timedelta(seconds=self.config.lease_duration_seconds)).isoformat()
        now_iso = current.isoformat()

        with self.storage.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE casefiles
                SET lease_owner = ?,
                    lease_expires_at = ?,
                    attempt_count = attempt_count + 1,
                    updated_at = ?
                WHERE casefile_id = ?
                  AND state NOT IN (?, ?, ?)
                  AND (
                        lease_owner IS NULL
                        OR lease_expires_at IS NULL
                        OR lease_expires_at <= ?
                      )
                """,
                (
                    self.config.worker_id,
                    expires_at,
                    now_iso,
                    casefile_id,
                    CaseState.SUBMITTED.value,
                    CaseState.REJECTED.value,
                    CaseState.TERMINAL_FAILED.value,
                    now_iso,
                ),
            )
            return cursor.rowcount == 1

    def release_lease(self, casefile_id: str) -> bool:
        """Releases a lease only if currently owned by this worker."""
        now_iso = datetime.now(timezone.utc).isoformat()
        with self.storage.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE casefiles
                SET lease_owner = NULL,
                    lease_expires_at = NULL,
                    updated_at = ?
                WHERE casefile_id = ?
                  AND lease_owner = ?
                """,
                (now_iso, casefile_id, self.config.worker_id),
            )
            return cursor.rowcount == 1

    def reclaim_expired_leases(self, now: datetime | None = None) -> list[str]:
        """
        Finds casefiles with expired leases (e.g., from a crashed process),
        clears their stale lease fields so they can be resumed safely, and returns
        the list of reclaimed casefile_ids.
        """
        current = now or datetime.now(timezone.utc)
        now_iso = current.isoformat()

        with self.storage.connect() as conn:
            rows = conn.execute(
                """
                SELECT casefile_id
                FROM casefiles
                WHERE lease_owner IS NOT NULL
                  AND lease_expires_at IS NOT NULL
                  AND lease_expires_at <= ?
                  AND state NOT IN (?, ?, ?)
                """,
                (
                    now_iso,
                    CaseState.SUBMITTED.value,
                    CaseState.REJECTED.value,
                    CaseState.TERMINAL_FAILED.value,
                ),
            ).fetchall()

            reclaimed_ids = [str(row["casefile_id"]) for row in rows]
            if reclaimed_ids:
                conn.execute(
                    """
                    UPDATE casefiles
                    SET lease_owner = NULL,
                        lease_expires_at = NULL,
                        updated_at = ?
                    WHERE lease_owner IS NOT NULL
                      AND lease_expires_at IS NOT NULL
                      AND lease_expires_at <= ?
                      AND state NOT IN (?, ?, ?)
                    """,
                    (
                        now_iso,
                        now_iso,
                        CaseState.SUBMITTED.value,
                        CaseState.REJECTED.value,
                        CaseState.TERMINAL_FAILED.value,
                    ),
                )
            return reclaimed_ids
