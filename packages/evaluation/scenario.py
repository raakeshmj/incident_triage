"""Golden incident scenarios: a deterministic world + a grading key.

A scenario has three parts with different audiences:

- `alerts` -- what fires. The only scenario content that reaches the
  model's context (as alerts on the incident, exactly like production).
- `world` -- the telemetry, change records and outages the evidence tools
  will return, relative to the moment the incident opens. The model sees
  it only by calling tools, through the real evidence service and adapters.
- `expected` (+ `title`, `description`, `notes`) -- the grading key. Never
  part of any model request; `leaked_expectations()` checks that.

Scenario files live in `evals/scenarios/*.json` and validate strictly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from packages.domain.investigation import CauseCategory, InvestigationBudget

ROOT = Path(__file__).resolve().parents[2]
SCENARIO_DIR = ROOT / "evals" / "scenarios"
EVAL_CATALOG = ROOT / "evals" / "catalog.json"

METRIC_NAMES = (
    "request_rate",
    "error_rate",
    "latency_p50",
    "latency_p95",
    "latency_p99",
    "dependency_error_rate",
    "cpu_usage",
    "memory_usage",
    "availability",
)
# A service nobody configured is healthy and idle-ish: these are what every
# metric reads unless the scenario says otherwise.
HEALTHY_DEFAULTS: dict[str, float] = {
    "request_rate": 50.0,
    "error_rate": 0.002,
    "latency_p50": 0.04,
    "latency_p95": 0.12,
    "latency_p99": 0.2,
    "dependency_error_rate": 0.001,
    "cpu_usage": 0.3,
    "memory_usage": 120 * 1024 * 1024,
    "availability": 1.0,
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Series(_Strict):
    """A metric that reads `baseline` until `change_at_min` (minutes relative
    to the incident opening; negative = before) and `incident` after."""

    baseline: float | None = None
    incident: float | None = None
    change_at_min: float = -10


class MetricSpec(_Strict):
    """One metric of one service. `by_dependency` gives per-dependency
    series (only for dependency_error_rate)."""

    series: Series | None = None
    by_dependency: dict[str, Series] = Field(default_factory=dict)


class LogLine(_Strict):
    at_min: float
    severity: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    message: str
    count: int = Field(default=1, ge=1, le=200)
    fields: dict[str, str] = Field(default_factory=dict)


class Deployment(_Strict):
    service: str
    at_min: float
    version: str
    previous_version: str
    commit_sha: str | None = None
    change_type: str = "rolling"


class ConfigChange(_Strict):
    service: str
    at_min: float
    key: str
    old_value: Any = None
    new_value: Any = None


class Commit(_Strict):
    at_min: float
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    author: str = "dev"
    subject: str
    files: list[str]


class World(_Strict):
    metrics: dict[str, dict[str, MetricSpec]] = Field(default_factory=dict)
    logs: dict[str, list[LogLine]] = Field(default_factory=dict)
    deployments: list[Deployment] = Field(default_factory=list)
    config_changes: list[ConfigChange] = Field(default_factory=list)
    commits: list[Commit] = Field(default_factory=list)
    # evidence sources that fail every query: prometheus, loki, tempo,
    # changes (deployment + config registries), git
    unavailable: list[Literal["prometheus", "loki", "tempo", "changes", "git"]] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def _known_metrics(self) -> World:
        for service, metrics in self.metrics.items():
            unknown = sorted(set(metrics) - set(METRIC_NAMES))
            if unknown:
                raise ValueError(f"{service}: unknown metrics {unknown}")
        return self


class Alert(_Strict):
    alertname: str
    severity: Literal["critical", "warning", "info"] = "critical"
    alert_type: str | None = None
    fired_at_min: float = 0
    summary: str = ""
    description: str = ""


class EvidenceMatcher(_Strict):
    """Matches evidence records by type, subject service and operation prefix
    (e.g. {"type": "metric", "service": "checkout-service",
    "operation": "metric_window:error_rate"})."""

    type: str
    service: str | None = None
    operation: str | None = None

    def matches(self, record: dict[str, Any]) -> bool:
        if record.get("evidence_type") != self.type:
            return False
        if self.service is not None and record.get("service") != self.service:
            return False
        return self.operation is None or str(record.get("operation", "")).startswith(self.operation)

    def label(self) -> str:
        return ":".join(p for p in (self.type, self.service, self.operation) if p)


class Cause(_Strict):
    categories: list[CauseCategory] = Field(min_length=1)
    component: str

    def matches(self, category: str | None, component: str | None) -> bool:
        return category in self.categories and component == self.component

    def label(self) -> str:
        return f"{'|'.join(self.categories)}@{self.component}"


class Expected(_Strict):
    outcome: Literal["RCA_READY", "ESCALATED"]
    root_cause: Cause | None = None
    # must be discovered (tool returned it) -- and, for RCA_READY, cited in
    # support of the selected hypothesis
    required_evidence: list[EvidenceMatcher] = Field(default_factory=list)
    # everything that counts toward evidence coverage
    acceptable_evidence: list[EvidenceMatcher] = Field(default_factory=list)
    # the alternatives a sound investigation considers and rules out
    competing_hypotheses: list[Cause] = Field(default_factory=list)
    # for ESCALATED scenarios: which escalation reasons are legitimate
    acceptable_escalation_reasons: list[str] = Field(
        default_factory=lambda: ["inconclusive", "budget_exhausted"]
    )

    @model_validator(mode="after")
    def _consistent(self) -> Expected:
        if self.outcome == "RCA_READY" and self.root_cause is None:
            raise ValueError("an RCA_READY scenario needs a root_cause")
        if self.outcome == "ESCALATED" and self.root_cause is not None:
            raise ValueError("an ESCALATED scenario has no single root cause")
        return self


class Lifecycle(_Strict):
    """What should happen after the investigation (Phase 8 lifecycle
    evaluation). A simulated human approves when `approve` is true."""

    expected_action: str | None = None
    approve: bool = True
    remediation_effective: bool = True
    expected_verification: Literal["PASSED", "FAILED", "TIMED_OUT"] | None = None
    expected_final_state: str


class Scenario(_Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,63}$")
    title: str
    kind: Literal["positive", "negative"]
    description: str  # the scenario's story, for humans -- never sent to a model
    service: str
    environment: str = "production"
    region: str = "us-east-1"
    initial_conditions: str
    observable_symptoms: list[str]
    alerts: list[Alert] = Field(min_length=1)
    world: World
    expected: Expected
    budget: dict[str, int] = Field(default_factory=dict)  # InvestigationBudget overrides
    lifecycle: Lifecycle | None = None
    notes: str = ""

    @model_validator(mode="after")
    def _kind_matches_outcome(self) -> Scenario:
        if (self.kind == "positive") != (self.expected.outcome == "RCA_READY"):
            raise ValueError("positive scenarios expect RCA_READY, negative ones ESCALATED")
        return self

    def investigation_budget(self, base: InvestigationBudget | None = None) -> InvestigationBudget:
        base = base or InvestigationBudget()
        return base.model_copy(update=self.budget)

    def grading_key_texts(self) -> list[str]:
        """Text that must never appear in anything sent to a model."""
        texts = [self.title, self.description, self.initial_conditions, self.notes]
        if self.expected.root_cause is not None:
            texts.append(self.expected.root_cause.label())
        return [t for t in texts if t and len(t) >= 12]


def load_scenario(path: str | Path) -> Scenario:
    return Scenario.model_validate(json.loads(Path(path).read_text()))


def load_scenarios(directory: str | Path = SCENARIO_DIR) -> dict[str, Scenario]:
    scenarios = [load_scenario(p) for p in sorted(Path(directory).glob("*.json"))]
    by_id = {s.id: s for s in scenarios}
    if len(by_id) != len(scenarios):
        raise ValueError("duplicate scenario ids")
    return by_id


def leaked_expectations(scenario: Scenario, model_inputs: list[str]) -> list[str]:
    """Grading-key texts found in any model input (should always be empty)."""
    blob = "\n".join(model_inputs)
    return [t for t in scenario.grading_key_texts() if t in blob]
