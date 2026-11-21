from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from job_workflow.schemas import CaseState, JobPosting, RunManifest


class WorkflowStorage:
    def __init__(self, db_path: Path, artifacts_dir: Path) -> None:
        self.db_path = db_path
        self.artifacts_dir = artifacts_dir
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS casefiles (
                    casefile_id TEXT PRIMARY KEY,
                    company TEXT NOT NULL,
                    title TEXT NOT NULL,
                    canonical_url TEXT NOT NULL,
                    state TEXT NOT NULL,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    tokens_spent INTEGER NOT NULL DEFAULT 0,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS step_ledger (
                    idempotency_key TEXT PRIMARY KEY,
                    casefile_id TEXT NOT NULL,
                    step_name TEXT NOT NULL,
                    output_json TEXT NOT NULL,
                    committed_at TEXT NOT NULL,
                    FOREIGN KEY (casefile_id) REFERENCES casefiles(casefile_id)
                );

                CREATE TABLE IF NOT EXISTS run_manifests (
                    run_id TEXT PRIMARY KEY,
                    manifest_json TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                """
            )

    def ingest_job_idempotent(self, posting: JobPosting) -> tuple[str, bool]:
        """
        Inserts a newly discovered job safely.
        Returns (casefile_id, created_new: bool). Duplicate re-runs return (id, False).
        """
        casefile_id = posting.compute_casefile_id()
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO casefiles (
                    casefile_id, company, title, canonical_url, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    casefile_id,
                    posting.company,
                    posting.title,
                    str(posting.canonical_url),
                    CaseState.DISCOVERED.value,
                    now,
                    now,
                ),
            )
            created = cursor.rowcount == 1

        if created:
            self.write_casefile_artifact(
                casefile_id,
                {"casefile_id": casefile_id, "state": CaseState.DISCOVERED.value, "posting": posting.model_dump(mode="json")},
            )
        return casefile_id, created

    def record_step_commit(
        self,
        casefile_id: str,
        step_name: str,
        next_state: CaseState,
        payload: dict,
        tokens_used: int = 0,
        schema_version: str = "v1",
    ) -> bool:
        """
        Commits a pipeline step atomically with an idempotency key.
        Returns True if committed now, False if this step was already committed previously.
        """
        idempotency_key = f"{casefile_id}:{step_name}:{schema_version}"
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO step_ledger (
                    idempotency_key, casefile_id, step_name, output_json, committed_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (idempotency_key, casefile_id, step_name, json.dumps(payload), now),
            )
            if cursor.rowcount == 0:
                return False

            conn.execute(
                """
                UPDATE casefiles
                SET state = ?,
                    tokens_spent = tokens_spent + ?,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    updated_at = ?
                WHERE casefile_id = ?
                """,
                (next_state.value, tokens_used, now, casefile_id),
            )
        return True

    def write_casefile_artifact(self, casefile_id: str, data: dict) -> Path:
        """Atomic write-to-temp-and-rename so partial disk writes never corrupt JSON artifacts."""
        target = self.artifacts_dir / f"{casefile_id}.json"
        fd, tmp_path = tempfile.mkstemp(dir=self.artifacts_dir, prefix=f".{casefile_id}_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
            os.replace(tmp_path, target)
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise
        return target

    def save_run_manifest(self, manifest: RunManifest) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO run_manifests (run_id, manifest_json, recorded_at)
                VALUES (?, ?, ?)
                """,
                (manifest.run_id, manifest.model_dump_json(indent=2), now),
            )
            