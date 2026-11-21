from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator


class CaseState(str, Enum):
    DISCOVERED = "DISCOVERED"
    NORMALIZED = "NORMALIZED"
    ENRICHED = "ENRICHED"
    SCORED = "SCORED"
    DRAFTED = "DRAFTED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SUBMITTED = "SUBMITTED"
    TERMINAL_FAILED = "TERMINAL_FAILED"


class JobPosting(BaseModel):
    """Canonical representation of a discovered job opportunity."""

    source: str = Field(..., min_length=1)
    external_id: str | None = None
    company: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    location: str = Field(default="Remote")
    canonical_url: HttpUrl
    raw_description: str = Field(..., min_length=20)
    discovered_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("company", "title", mode="before")
    @classmethod
    def strip_whitespace(cls, v: str) -> str:
        return " ".join(str(v).strip().split())

    def compute_casefile_id(self) -> str:
        """Deterministic SHA-256 fingerprint to prevent duplicate casefiles across sweeps."""
        normalized_company = self.company.lower().strip()
        normalized_title = self.title.lower().strip()
        anchor = (self.external_id or str(self.canonical_url)).lower().strip()
        payload = f"{normalized_company}|{normalized_title}|{anchor}".encode("utf-8")
        return hashlib.sha256(payload).hexdigest()[:24]


class FitEvaluation(BaseModel):
    """Validated structured output from the role-scoring agent step."""

    overall_score: float = Field(..., ge=0.0, le=100.0)
    recommendation: Literal["STRONG_APPLY", "CONDITIONAL_APPLY", "SKIP"]
    matched_skills: list[str] = Field(default_factory=list)
    missing_gaps: list[str] = Field(default_factory=list)
    evidence_citations: list[str] = Field(..., min_length=1)
    rationale: str = Field(..., min_length=20)


class ApplicationPacket(BaseModel):
    """Human-in-the-loop review packet frozen prior to any external side effect."""

    casefile_id: str
    tailored_summary: str = Field(..., min_length=30)
    resume_bullets: list[str] = Field(..., min_length=2, max_length=6)
    outreach_draft: str = Field(..., min_length=30)
    approval_status: Literal["PENDING", "APPROVED", "REJECTED"] = "PENDING"
    reviewer_notes: str | None = None


class RunManifest(BaseModel):
    """Verifiable audit summary emitted by every scheduled or manual workflow run."""

    run_id: str
    started_at: datetime
    finished_at: datetime | None = None
    discovered_count: int = 0
    duplicates_skipped: int = 0
    transitions_committed: int = 0
    approvals_queued: int = 0
    tokens_consumed: int = 0
    errors: list[str] = Field(default_factory=list)
