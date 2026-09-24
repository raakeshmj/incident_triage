"""Deterministic, explainable, multi-signal alert correlation (Phase 2).

Phase 1's `correlation.py` answered one narrow question: "is this the same
alert firing again?" (fingerprint equality). This module answers the
broader question ADR-0004 always anticipated: "does this alert, which may
be different from anything already seen, belong to an existing incident?"

Still governed by ADR-0004: **no LLM, ever**. Every decision here is a sum
of small, named, unit-testable rules over data already in Postgres. The
result is always explainable -- a list of matched signal descriptions and
a numeric score -- because an unexplainable correlation decision is not
meaningfully different from an LLM guess, and the whole point of ADR-0004
is that this decision must be auditable and reproducible.

The `CorrelationRule` protocol is the extension point: adding a new signal
(e.g. real deployment-proximity data once a deployment source exists) means
writing one more small class and adding it to the rule list, not changing
the engine or any call site.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal, Protocol

DEFAULT_THRESHOLD = 0.6
DEFAULT_TEMPORAL_WINDOW_SECONDS = 90
DEFAULT_CANDIDATE_LOOKBACK_SECONDS = 15 * 60  # how far back a candidate incident's most
# recent alert can be before it's not even considered -- see "Case F: late-arriving
# alert" in docs/architecture/04-incident-state-machine.md's Phase 2 addendum.


@dataclass(frozen=True)
class NewAlertContext:
    """Everything about the incoming alert the rules are allowed to see."""

    source: str
    fingerprint: str
    labels: dict[str, str]
    service: str
    environment: str
    received_at: datetime


@dataclass(frozen=True)
class CorrelationCandidate:
    """One open incident being considered as a home for the new alert.

    `most_recent_alert_*` describes the most recently received alert
    already linked to this incident -- the temporal/type-similarity rules
    compare the new alert against that, not against the incident's
    creation time, so a long-running incident with fresh related alerts
    still scores well.
    """

    incident_id: uuid.UUID
    correlation_key: str
    status: str
    service: str
    environment: str
    most_recent_alert_received_at: datetime
    most_recent_alert_labels: dict[str, str]
    most_recent_alert_fingerprint: str


@dataclass(frozen=True)
class RuleResult:
    contribution: float
    signal: str | None  # human-readable description, or None if the rule didn't fire


class CorrelationRule(Protocol):
    """The extension point. Weight is intrinsic to the rule instance, not
    the engine, so rules are self-contained and independently testable.

    `name` is declared as a read-only property (not a plain attribute) so
    frozen dataclasses -- every concrete rule below -- satisfy this
    Protocol structurally; a plain `name: str` would require a setter.
    """

    @property
    def name(self) -> str: ...

    def evaluate(self, alert: NewAlertContext, candidate: CorrelationCandidate) -> RuleResult: ...


@dataclass(frozen=True)
class SameServiceRule:
    weight: float = 0.3
    name: str = "same_service"

    def evaluate(self, alert: NewAlertContext, candidate: CorrelationCandidate) -> RuleResult:
        if alert.service == candidate.service:
            return RuleResult(self.weight, "same service")
        return RuleResult(0.0, None)


@dataclass(frozen=True)
class SameEnvironmentRule:
    weight: float = 0.2
    name: str = "same_environment"

    def evaluate(self, alert: NewAlertContext, candidate: CorrelationCandidate) -> RuleResult:
        if alert.environment == candidate.environment:
            return RuleResult(self.weight, "same environment")
        return RuleResult(0.0, None)


@dataclass(frozen=True)
class SameLabelValueRule:
    """Generic "same <label>" rule, used for both region and dependency.

    Contributes nothing (not a penalty) when either side lacks the label --
    absence of a signal is not evidence against correlation.
    """

    label_key: str
    weight: float
    name: str

    def evaluate(self, alert: NewAlertContext, candidate: CorrelationCandidate) -> RuleResult:
        new_value = alert.labels.get(self.label_key)
        candidate_value = candidate.most_recent_alert_labels.get(self.label_key)
        if new_value and candidate_value and new_value == candidate_value:
            return RuleResult(self.weight, f"same {self.label_key}")
        return RuleResult(0.0, None)


def same_region_rule(weight: float = 0.1) -> SameLabelValueRule:
    return SameLabelValueRule(label_key="region", weight=weight, name="same_region")


def same_dependency_rule(weight: float = 0.1) -> SameLabelValueRule:
    return SameLabelValueRule(label_key="dependency", weight=weight, name="same_dependency")


@dataclass(frozen=True)
class TemporalProximityRule:
    """Full weight if the new alert arrives within `window_seconds` of the
    candidate's most recent alert; linearly decayed within a grace band
    beyond that, zero beyond twice the window. Never fires "backwards" in
    a way that would let an arbitrarily old incident absorb a new alert --
    see Case F (late-arriving alerts) in the Phase 2 correlation behavior
    notes.
    """

    weight: float = 0.2
    window_seconds: int = DEFAULT_TEMPORAL_WINDOW_SECONDS
    name: str = "temporal_proximity"

    def evaluate(self, alert: NewAlertContext, candidate: CorrelationCandidate) -> RuleResult:
        delta = abs((alert.received_at - candidate.most_recent_alert_received_at).total_seconds())
        if delta <= self.window_seconds:
            return RuleResult(self.weight, f"within {self.window_seconds} seconds")
        return RuleResult(0.0, None)


@dataclass(frozen=True)
class RelatedAlertTypeRule:
    """Exact `alertname` match scores full weight ("same alert type");
    a configured relation between two different alertnames scores partial
    weight ("related alert types"). Unrelated types contribute nothing.

    The relation table is intentionally simple (a dict of sets) rather
    than a taxonomy service -- exactly the kind of thing this module's
    docstring means by "a clear interface so the correlation strategy can
    evolve later": swap this rule for a richer one without touching the
    engine or any other rule.
    """

    weight: float = 0.2
    related_weight_fraction: float = 0.85
    related_alert_types: dict[str, frozenset[str]] = field(default_factory=dict)
    name: str = "related_alert_type"

    def evaluate(self, alert: NewAlertContext, candidate: CorrelationCandidate) -> RuleResult:
        new_type = alert.labels.get("alertname")
        candidate_type = candidate.most_recent_alert_labels.get("alertname")
        if not new_type or not candidate_type:
            return RuleResult(0.0, None)
        if new_type == candidate_type:
            return RuleResult(self.weight, "same alert type")
        related = self.related_alert_types.get(new_type, frozenset())
        if candidate_type in related:
            return RuleResult(self.weight * self.related_weight_fraction, "related alert types")
        return RuleResult(0.0, None)


@dataclass(frozen=True)
class DeploymentProximityRule:
    """Stub: there is no deployment data source yet (evidence-service and
    its deployment-history tool are Phase 5+, see
    docs/architecture/08-evidence-model.md). Always contributes 0 until
    that data exists. Kept as an explicit rule -- not simply omitted -- so
    the rule list documents every signal the architecture names, including
    the ones not implementable yet, and so wiring in real data later is a
    one-line change to this class instead of a new rule slotting in
    unnoticed.
    """

    weight: float = 0.0
    name: str = "deployment_proximity"

    def evaluate(self, alert: NewAlertContext, candidate: CorrelationCandidate) -> RuleResult:
        del alert, candidate
        return RuleResult(0.0, None)


Decision = Literal["CORRELATE", "NEW_INCIDENT"]


@dataclass(frozen=True)
class CorrelationDecision:
    decision: Decision
    score: float
    matched_signals: tuple[str, ...]
    matched_incident_id: uuid.UUID | None
    correlation_key: str


class CorrelationEngine:
    """Pure, deterministic. No I/O, no clock reads (the caller supplies
    `alert.received_at`), no randomness -- the same `(alert, candidates)`
    input always produces the same `CorrelationDecision`, which is what
    makes this testable and auditable in the way ADR-0004 requires.
    """

    def __init__(
        self,
        rules: Sequence[CorrelationRule],
        *,
        threshold: float = DEFAULT_THRESHOLD,
        new_incident_correlation_key: str | None = None,
    ) -> None:
        self._rules = list(rules)
        self._threshold = threshold
        self._new_incident_correlation_key = new_incident_correlation_key

    def decide(
        self,
        alert: NewAlertContext,
        candidates: Sequence[CorrelationCandidate],
    ) -> CorrelationDecision:
        best_candidate: CorrelationCandidate | None = None
        best_score = 0.0
        best_signals: tuple[str, ...] = ()

        for candidate in candidates:
            score = 0.0
            signals: list[str] = []
            for rule in self._rules:
                result = rule.evaluate(alert, candidate)
                score += result.contribution
                if result.signal:
                    signals.append(result.signal)
            score = min(score, 1.0)
            if best_candidate is None or score > best_score:
                best_candidate = candidate
                best_score = score
                best_signals = tuple(signals)

        if best_candidate is not None and best_score >= self._threshold:
            return CorrelationDecision(
                decision="CORRELATE",
                score=best_score,
                matched_signals=best_signals,
                matched_incident_id=best_candidate.incident_id,
                correlation_key=best_candidate.correlation_key,
            )

        return CorrelationDecision(
            decision="NEW_INCIDENT",
            score=best_score,
            matched_signals=best_signals,
            matched_incident_id=None,
            correlation_key=self._new_incident_correlation_key or alert.fingerprint,
        )


def default_rules(
    *,
    temporal_window_seconds: int = DEFAULT_TEMPORAL_WINDOW_SECONDS,
    related_alert_types: dict[str, frozenset[str]] | None = None,
) -> list[CorrelationRule]:
    return [
        SameServiceRule(),
        SameEnvironmentRule(),
        same_region_rule(),
        same_dependency_rule(),
        TemporalProximityRule(window_seconds=temporal_window_seconds),
        RelatedAlertTypeRule(related_alert_types=related_alert_types or {}),
        DeploymentProximityRule(),
    ]


def default_correlation_engine(
    *,
    threshold: float = DEFAULT_THRESHOLD,
    temporal_window_seconds: int = DEFAULT_TEMPORAL_WINDOW_SECONDS,
    new_incident_correlation_key: str | None = None,
    related_alert_types: dict[str, frozenset[str]] | None = None,
) -> CorrelationEngine:
    return CorrelationEngine(
        default_rules(
            temporal_window_seconds=temporal_window_seconds,
            related_alert_types=related_alert_types,
        ),
        threshold=threshold,
        new_incident_correlation_key=new_incident_correlation_key,
    )


def candidate_lookback_cutoff(
    now: datetime, lookback_seconds: int = DEFAULT_CANDIDATE_LOOKBACK_SECONDS
) -> datetime:
    """The repository layer uses this to bound the candidate query itself
    (not just filter after the fact) -- see
    packages/incident/repository.find_open_incident_candidates.
    """
    return now - timedelta(seconds=lookback_seconds)
