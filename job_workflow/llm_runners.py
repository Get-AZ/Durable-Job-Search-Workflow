from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Callable, Protocol
from urllib.request import Request, urlopen

from job_workflow.pipeline import StructuredStepRunner

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)

HttpPostJson = Callable[[str, dict[str, str], str], str]


def default_http_post_json(url: str, headers: dict[str, str], body: str) -> str:
    req = Request(
        url=url,
        data=body.encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urlopen(req, timeout=30) as resp:  # noqa: S310
        return resp.read().decode("utf-8")


def extract_json_payload(raw_text: str) -> str:
    """Strips optional markdown code fences around model JSON responses."""
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = _FENCE_RE.sub("", cleaned).strip()
    return cleaned


class ChatCompletionClient(Protocol):
    def complete_json(self, system_prompt: str, user_prompt: str) -> tuple[str, int]:
        """Returns (raw_response_text, tokens_used) for a given system and user prompt."""


@dataclass(frozen=True)
class OpenAICompatibleConfig:
    api_key: str
    model: str = "gpt-4o-mini"
    base_url: str = "[https://api.openai.com/v1](https://api.openai.com/v1)"
    temperature: float = 0.1

    @classmethod
    def from_env(cls, model: str | None = None) -> "OpenAICompatibleConfig":
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise ValueError("OPENAI_API_KEY environment variable is not set.")
        base_url = os.environ.get("OPENAI_BASE_URL", "[https://api.openai.com/v1](https://api.openai.com/v1)").strip().rstrip("/")
        resolved_model = (model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")).strip()
        return cls(
            api_key=api_key,
            model=resolved_model,
            base_url=base_url,
        )


class OpenAICompatibleClient:
    """Calls an OpenAI-compatible /v1/chat/completions endpoint and reports total tokens."""

    def __init__(
        self,
        config: OpenAICompatibleConfig,
        poster: HttpPostJson = default_http_post_json,
    ) -> None:
        self.config = config
        self.poster = poster

    def complete_json(self, system_prompt: str, user_prompt: str) -> tuple[str, int]:
        endpoint = f"{self.config.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        payload = json.dumps(
            {
                "model": self.config.model,
                "temperature": self.config.temperature,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            }
        )
        raw_response = self.poster(endpoint, headers, payload)
        parsed = json.loads(raw_response)

        choices = parsed.get("choices", [])
        if not choices or not isinstance(choices, list):
            raise ValueError("Chat completion response contained no choices.")
        message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
        content = str(message.get("content", "")).strip()

        usage = parsed.get("usage", {}) if isinstance(parsed, dict) else {}
        tokens_used = int(usage.get("total_tokens", 0) or 0)
        return content, tokens_used


@dataclass(frozen=True)
class CandidateProfile:
    headline: str
    core_skills: list[str]
    highlights: list[str]


class LLMStepRunners:
    """Builds score_runner and draft_runner callables compatible with DurableCasefilePipeline."""

    def __init__(self, client: ChatCompletionClient, profile: CandidateProfile) -> None:
        self.client = client
        self.profile = profile

    def score_runner(self, context: dict[str, object], repair_feedback: str | None) -> tuple[str, int]:
        system_prompt = (
            "You are a precise role-fit evaluator. Return ONLY a valid JSON object matching:\n"
            "{\n"
            '  "overall_score": <float 0.0..100.0>,\n'
            '  "recommendation": "STRONG_APPLY" | "CONDITIONAL_APPLY" | "SKIP",\n'
            '  "matched_skills": [string, ...],\n'
            '  "missing_gaps": [string, ...],\n'
            '  "evidence_citations": [string, ...],\n'
            '  "rationale": <string >= 20 chars>\n'
            "}"
        )
        user_payload = {
            "candidate_profile": {
                "headline": self.profile.headline,
                "core_skills": self.profile.core_skills,
                "highlights": self.profile.highlights,
            },
            "role_context": context,
            "repair_feedback": repair_feedback,
        }
        raw_text, tokens_used = self.client.complete_json(system_prompt, json.dumps(user_payload))
        return extract_json_payload(raw_text), tokens_used

    def draft_runner(self, context: dict[str, object], repair_feedback: str | None) -> tuple[str, int]:
        system_prompt = (
            "You are a concise career packet drafter. Return ONLY a valid JSON object matching:\n"
            "{\n"
            '  "casefile_id": <string>,\n'
            '  "tailored_summary": <string >= 30 chars>,\n'
            '  "resume_bullets": [<string>, ...] (2 to 6 items),\n'
            '  "outreach_draft": <string >= 30 chars>,\n'
            '  "approval_status": "PENDING",\n'
            '  "reviewer_notes": <string | null>\n'
            "}"
        )
        user_payload = {
            "candidate_profile": {
                "headline": self.profile.headline,
                "core_skills": self.profile.core_skills,
                "highlights": self.profile.highlights,
            },
            "draft_context": context,
            "repair_feedback": repair_feedback,
        }
        raw_text, tokens_used = self.client.complete_json(system_prompt, json.dumps(user_payload))
        return extract_json_payload(raw_text), tokens_used

    def as_pair(self) -> tuple[StructuredStepRunner, StructuredStepRunner]:
        return self.score_runner, self.draft_runner
