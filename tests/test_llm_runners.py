import json

import pytest

from job_workflow.limits import BudgetPolicy, RunBudgetTracker
from job_workflow.llm_runners import (
    CandidateProfile,
    LLMStepRunners,
    OpenAICompatibleClient,
    OpenAICompatibleConfig,
    extract_json_payload,
)
from job_workflow.pipeline import run_validated_json_step
from job_workflow.schemas import FitEvaluation


class StubClient:
    def __init__(self, responses: list[tuple[str, int]]) -> None:
        self._responses = list(responses)
        self.seen_prompts: list[str] = []

    def complete_json(self, system_prompt: str, user_prompt: str) -> tuple[str, int]:
        _ = system_prompt
        self.seen_prompts.append(user_prompt)
        return self._responses.pop(0)


def test_extract_json_payload_strips_code_fence() -> None:
    fenced = '```json\n{"ok": true}\n```'
    assert extract_json_payload(fenced) == '{"ok": true}'


def test_score_runner_strips_fences_and_repairs_invalid_first_output() -> None:
    bad_json = "```json\n{\"overall_score\": 90.0}\n```"
    good_json = (
        "```json\n"
        + json.dumps(
            {
                "overall_score": 89.0,
                "recommendation": "STRONG_APPLY",
                "matched_skills": ["Python", "FastAPI"],
                "missing_gaps": [],
                "evidence_citations": ["Built durable workflow engine in Python"],
                "rationale": "Strong match for AI systems and reliability engineering roles.",
            }
        )
        + "\n```"
    )
    client = StubClient([(bad_json, 110), (good_json, 140)])
    profile = CandidateProfile(
        headline="AI & Systems Engineer",
        core_skills=["Python", "FastAPI", "SQLite"],
        highlights=["Built idempotent durable workflows with Pydantic validation"],
    )
    runners = LLMStepRunners(client=client, profile=profile)
    budget = RunBudgetTracker(policy=BudgetPolicy(max_run_tokens=5000, max_casefile_tokens=5000))

    result, tokens = run_validated_json_step(
        casefile_id="cf-llm-1",
        step_name="score",
        context={"company": "Acme", "title": "AI Engineer"},
        model_cls=FitEvaluation,
        runner=runners.score_runner,
        budget=budget,
    )

    assert isinstance(result, FitEvaluation)
    assert result.overall_score == 89.0
    assert tokens == 250
    assert "failed schema validation" in client.seen_prompts[1]


def test_openai_compatible_client_parses_content_and_tokens() -> None:
    captured: dict[str, object] = {}

    def fake_post(url: str, headers: dict[str, str], body: str) -> str:
        captured["url"] = url
        captured["auth"] = headers.get("Authorization")
        captured["body"] = json.loads(body)
        return json.dumps(
            {
                "choices": [{"message": {"content": '{"hello": "world"}'}}],
                "usage": {"total_tokens": 312},
            }
        )

    cfg = OpenAICompatibleConfig(
        api_key="sk-test-key",
        model="gpt-4o-mini",
        base_url="https://api.example.test/v1",
    )
    client = OpenAICompatibleClient(config=cfg, poster=fake_post)
    content, tokens = client.complete_json("sys", "usr")

    assert content == '{"hello": "world"}'
    assert tokens == 312
    assert captured["url"] == "https://api.example.test/v1/chat/completions"
    assert captured["auth"] == "Bearer sk-test-key"


def test_openai_compatible_config_from_env_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        OpenAICompatibleConfig.from_env()
