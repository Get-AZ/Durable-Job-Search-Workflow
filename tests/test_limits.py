from datetime import datetime, timedelta, timezone
import pytest

from job_workflow.limits import (
    BudgetExceededError,
    BudgetPolicy,
    CircuitOpenError,
    RunBudgetTracker,
)


def test_token_and_hop_limits_enforced() -> None:
    tracker = RunBudgetTracker(
        policy=BudgetPolicy(
            max_run_tokens=1000,
            max_casefile_tokens=600,
            max_research_hops_per_casefile=2,
        )
    )
    tracker.consume_tokens("cf-1", 400)
    tracker.consume_research_hop("cf-1", 2)

    with pytest.raises(BudgetExceededError):
        tracker.consume_research_hop("cf-1", 1)

    with pytest.raises(BudgetExceededError):
        tracker.consume_tokens("cf-1", 300)


def test_circuit_breaker_trips_and_recovers_after_cooldown() -> None:
    tracker = RunBudgetTracker(
        policy=BudgetPolicy(circuit_failure_threshold=2, circuit_cooldown_seconds=30)
    )
    t0 = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)

    tracker.record_failure(now=t0)
    tracker.record_failure(now=t0)

    with pytest.raises(CircuitOpenError):
        tracker.check_circuit(now=t0 + timedelta(seconds=10))

    # After cooldown elapses, circuit allows recovery check
    tracker.check_circuit(now=t0 + timedelta(seconds=31))
    tracker.consume_api_call(count=1, now=t0 + timedelta(seconds=31))
