from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


class BudgetExceededError(RuntimeError):
    """Raised when a run or casefile exceeds an enforced token, call, hop, or attempt limit."""


class CircuitOpenError(RuntimeError):
    """Raised when a circuit breaker trips due to consecutive upstream failures."""


@dataclass(frozen=True)
class BudgetPolicy:
    max_run_tokens: int = 50_000
    max_casefile_tokens: int = 8_000
    max_api_calls_per_run: int = 100
    max_research_hops_per_casefile: int = 4
    max_attempts_per_casefile: int = 3
    circuit_failure_threshold: int = 3
    circuit_cooldown_seconds: int = 60


@dataclass
class RunBudgetTracker:
    policy: BudgetPolicy = field(default_factory=BudgetPolicy)
    run_tokens_used: int = 0
    api_calls_made: int = 0
    casefile_tokens_used: dict[str, int] = field(default_factory=dict)
    casefile_hops_used: dict[str, int] = field(default_factory=dict)
    casefile_attempts_used: dict[str, int] = field(default_factory=dict)
    consecutive_failures: int = 0
    circuit_open_until: datetime | None = None

    def check_circuit(self, now: datetime | None = None) -> None:
        current = now or datetime.now(timezone.utc)
        if self.circuit_open_until is not None:
            if current < self.circuit_open_until:
                raise CircuitOpenError(
                    f"Circuit open until {self.circuit_open_until.isoformat()} after "
                    f"{self.consecutive_failures} consecutive failures."
                )
            # Cooldown elapsed: transition to half-open trial state
            self.circuit_open_until = None
            self.consecutive_failures = 0

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.circuit_open_until = None

    def record_failure(self, now: datetime | None = None) -> None:
        current = now or datetime.now(timezone.utc)
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.policy.circuit_failure_threshold:
            self.circuit_open_until = current + timedelta(
                seconds=self.policy.circuit_cooldown_seconds
            )

    def consume_api_call(self, count: int = 1, now: datetime | None = None) -> None:
        self.check_circuit(now=now)
        if self.api_calls_made + count > self.policy.max_api_calls_per_run:
            raise BudgetExceededError(
                f"Run API call budget exceeded: {self.api_calls_made + count} > "
                f"{self.policy.max_api_calls_per_run}"
            )
        self.api_calls_made += count

    def consume_tokens(self, casefile_id: str, tokens: int) -> None:
        if tokens < 0:
            raise ValueError("Token delta cannot be negative.")
        next_run_tokens = self.run_tokens_used + tokens
        if next_run_tokens > self.policy.max_run_tokens:
            raise BudgetExceededError(
                f"Run token budget exceeded: {next_run_tokens} > {self.policy.max_run_tokens}"
            )
        next_case_tokens = self.casefile_tokens_used.get(casefile_id, 0) + tokens
        if next_case_tokens > self.policy.max_casefile_tokens:
            raise BudgetExceededError(
                f"Casefile '{casefile_id}' token budget exceeded: "
                f"{next_case_tokens} > {self.policy.max_casefile_tokens}"
            )
        self.run_tokens_used = next_run_tokens
        self.casefile_tokens_used[casefile_id] = next_case_tokens

    def consume_research_hop(self, casefile_id: str, hops: int = 1) -> None:
        next_hops = self.casefile_hops_used.get(casefile_id, 0) + hops
        if next_hops > self.policy.max_research_hops_per_casefile:
            raise BudgetExceededError(
                f"Casefile '{casefile_id}' research hop limit exceeded: "
                f"{next_hops} > {self.policy.max_research_hops_per_casefile}"
            )
        self.casefile_hops_used[casefile_id] = next_hops

    def consume_attempt(self, casefile_id: str) -> int:
        next_attempts = self.casefile_attempts_used.get(casefile_id, 0) + 1
        if next_attempts > self.policy.max_attempts_per_casefile:
            raise BudgetExceededError(
                f"Casefile '{casefile_id}' max attempts exceeded: "
                f"{next_attempts} > {self.policy.max_attempts_per_casefile}"
            )
        self.casefile_attempts_used[casefile_id] = next_attempts
        return next_attempts
