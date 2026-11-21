import json
import pytest

from job_workflow.limits import BudgetPolicy, RunBudgetTracker
from job_workflow.pipeline import SchemaRepairExhaustedError, run_validated_json_step
from job_workflow.schemas import FitEvaluation


def test_repair_loop_succeeds_after_initial_invalid_json() -> None:
    calls: list[str | None] = []

    def flaky_runner(_ctx: dict[str, object], repair_feedback: str | None) -> tuple[str, int]:
        calls.append(repair_feedback)
        if repair_feedback is None:
            # Missing required 'evidence_citations' and score > 100
            return json.dumps({"overall_score": 150.0, "recommendation": "STRONG_APPLY", "rationale": "short"}), 100
        return json.dumps(
            {
                "overall_score": 88.5,
                "recommendation": "STRONG_APPLY",
                "matched_skills": ["Python", "SQLite"],
                "missing_gaps": [],
                "evidence_citations": ["Built state-machine workflows in Python"],
                "rationale": "Strong alignment with durable workflow and Python reliability requirements.",
            }
        ), 120

    budget = RunBudgetTracker(policy=BudgetPolicy(max_run_tokens=5000, max_casefile_tokens=5000))
    result, tokens = run_validated_json_step(
        casefile_id="cf-100",
        step_name="score",
        context={},
        model_cls=FitEvaluation,
        runner=flaky_runner,
        budget=budget,
    )

    assert isinstance(result, FitEvaluation)
    assert result.overall_score == 88.5
    assert tokens == 220
    assert len(calls) == 2
    assert calls[0] is None
    assert calls[1] is not None


def test_repair_loop_raises_when_exhausted() -> None:
    def always_bad(_ctx: dict[str, object], _feedback: str | None) -> tuple[str, int]:
        return "not-valid-json", 50

    budget = RunBudgetTracker(policy=BudgetPolicy(max_run_tokens=5000, max_casefile_tokens=5000))
    with pytest.raises(SchemaRepairExhaustedError):
        run_validated_json_step(
            casefile_id="cf-101",
            step_name="score",
            context={},
            model_cls=FitEvaluation,
            runner=always_bad,
            budget=budget,
            max_repair_attempts=1,
        )
