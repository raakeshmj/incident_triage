"""Investigation domain: lifecycle, hypotheses, budgets, the model-facing
schemas, and the deterministic rules that decide what model output may
change (docs/architecture/07-agent-tool-architecture.md, ADR-0020).

Pure: no model SDK, no database, no telemetry backend. Everything the model
proposes is validated here (schema) and in incident-core (evidence
existence) before it touches state.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

# --- lifecycle ------------------------------------------------------------------


class InvestigationStatus(str, Enum):
    CREATED = "CREATED"
    INVESTIGATING = "INVESTIGATING"
    COMPLETED = "COMPLETED"  # root cause selected; incident -> RCA_READY
    FAILED = "FAILED"  # technical failure (model/tooling); incident -> ESCALATED
    ESCALATED = "ESCALATED"  # ran, could not conclude (inconclusive/budget); incident -> ESCALATED


TERMINAL_INVESTIGATION_STATUSES = frozenset(
    {InvestigationStatus.COMPLETED, InvestigationStatus.FAILED, InvestigationStatus.ESCALATED}
)


class HypothesisStatus(str, Enum):
    ACTIVE = "ACTIVE"
    SUPPORTED = "SUPPORTED"
    WEAKENED = "WEAKENED"
    REJECTED = "REJECTED"  # terminal: a rejected explanation is never revived
    SELECTED = "SELECTED"  # set only by incident-core on an accepted conclusion


MODEL_SETTABLE_HYPOTHESIS_STATUSES = ("ACTIVE", "SUPPORTED", "WEAKENED", "REJECTED")


class StepKind(str, Enum):
    """Kinds of rows in the investigation trace (`investigation_steps`)."""

    CONTEXT = "context"  # the initial context the model was given
    MODEL_TURN = "model_turn"  # one model response (normalized + provider payload)
    TOOL_CALL = "tool_call"  # an evidence tool call and what the model saw back
    HYPOTHESIS_UPDATE = "hypothesis_update"  # update_hypotheses call: applied / rejected
    CONCLUSION_REJECTED = "conclusion_rejected"  # conclude failed validation/criteria
    INVALID_CALL = "invalid_call"  # unknown tool / malformed arguments
    FEEDBACK = "feedback"  # engine -> model notice (no tool call, final turn, ...)
    MODEL_ERROR = "model_error"  # an API failure (retried or terminal)
    OUTCOME = "outcome"  # terminal: completed / escalated / failed


# Step kinds that answer a specific model tool call (carry `call_id`).
ACTION_STEP_KINDS = frozenset(
    {
        StepKind.TOOL_CALL,
        StepKind.HYPOTHESIS_UPDATE,
        StepKind.CONCLUSION_REJECTED,
        StepKind.INVALID_CALL,
    }
)


# --- budgets & stopping criteria --------------------------------------------------


class InvestigationBudget(BaseModel):
    """Investigation-level budgets. Per-call limits (window sizes, result
    counts) live in the evidence service and tool layer, not here."""

    model_config = ConfigDict(frozen=True)

    max_iterations: int = Field(default=15, ge=2, le=100)
    max_tool_calls: int = Field(default=25, ge=1, le=200)
    max_identical_tool_calls: int = Field(default=2, ge=1, le=10)
    max_evidence_items: int = Field(default=40, ge=1, le=500)
    max_wall_clock_seconds: int = Field(default=900, ge=10)
    max_total_tokens: int | None = Field(default=600_000, ge=1_000)
    max_consecutive_invalid_turns: int = Field(default=3, ge=1)
    max_consecutive_tool_failures: int = Field(default=5, ge=1)


class StoppingCriteria(BaseModel):
    """What an application-side check requires before a conclusion is
    accepted. Model confidence is one input, never sufficient on its own."""

    model_config = ConfigDict(frozen=True)

    min_hypotheses_considered: int = Field(default=2, ge=1)
    min_supporting_evidence: int = Field(default=2, ge=1)
    min_supporting_evidence_types: int = Field(default=2, ge=1)
    min_confidence: float = Field(default=0.6, ge=0, le=1)


# --- model-facing schemas ---------------------------------------------------------

_KEY = r"^[A-Za-z][A-Za-z0-9_-]{0,15}$"
_COMPONENT = r"^[a-z0-9][a-z0-9._-]{0,62}$"

# A small, generic cause taxonomy. It names *kinds* of cause, never any
# incident's answer, and makes the selected root cause gradeable as
# structured data (category + component) instead of by prose similarity.
CAUSE_CATEGORIES = (
    "deployment",
    "configuration",
    "code_change",
    "dependency",
    "database",
    "resource_cpu",
    "resource_memory",
    "traffic",
    "infrastructure",
    "other",
)
CauseCategory = Literal[
    "deployment",
    "configuration",
    "code_change",
    "dependency",
    "database",
    "resource_cpu",
    "resource_memory",
    "traffic",
    "infrastructure",
    "other",
]
EvidenceIdList = list[uuid.UUID]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HypothesisUpdate(_Strict):
    """Create or change one hypothesis. Evidence ids are *added*; a
    hypothesis never loses evidence it has been linked to."""

    key: str = Field(pattern=_KEY, description="Short stable id you choose, e.g. H1.")
    description: str | None = Field(
        default=None,
        min_length=1,
        max_length=500,
        description="Required when creating a hypothesis. A claim to test, not a conclusion.",
    )
    cause_category: CauseCategory | None = Field(
        default=None,
        description="Required when creating a hypothesis: the kind of cause it proposes. "
        "Fixed once set.",
    )
    component: str | None = Field(
        default=None,
        pattern=_COMPONENT,
        description="Required when creating a hypothesis: the service or component "
        "the hypothesis says is at fault. Fixed once set.",
    )
    status: Literal["ACTIVE", "SUPPORTED", "WEAKENED", "REJECTED"] | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    supporting_evidence_ids: EvidenceIdList = Field(default_factory=list, max_length=20)
    contradicting_evidence_ids: EvidenceIdList = Field(default_factory=list, max_length=20)
    missing_evidence: list[str] | None = Field(
        default=None,
        max_length=10,
        description="What evidence would confirm or refute this (the evidence gap).",
    )
    rationale: str = Field(
        min_length=1, max_length=1000, description="Why, citing what you observed."
    )


class HypothesisUpdateBatch(_Strict):
    updates: list[HypothesisUpdate] = Field(min_length=1, max_length=10)


class GroundedStatement(_Strict):
    text: str = Field(min_length=1, max_length=1000)
    evidence_ids: EvidenceIdList = Field(min_length=1, max_length=20)


class AffectedService(_Strict):
    service: str = Field(min_length=1, max_length=64)
    evidence_ids: EvidenceIdList = Field(min_length=1, max_length=20)


class TimelineEntry(_Strict):
    at: AwareDatetime
    event: str = Field(min_length=1, max_length=500)
    evidence_ids: EvidenceIdList = Field(min_length=1, max_length=20)


class ContradictionNote(_Strict):
    evidence_id: uuid.UUID
    explanation: str = Field(min_length=1, max_length=1000)


class RcaDraft(_Strict):
    """Every factual section is a grounded statement; only the explicitly
    open-ended sections (questions, next diagnostic step) are free text."""

    incident_summary: GroundedStatement
    impact: GroundedStatement
    affected_services: list[AffectedService] = Field(min_length=1, max_length=10)
    timeline: list[TimelineEntry] = Field(min_length=1, max_length=30)
    root_cause: GroundedStatement
    contributing_factors: list[GroundedStatement] = Field(default_factory=list, max_length=10)
    contradicting_evidence: list[ContradictionNote] = Field(default_factory=list, max_length=20)
    unresolved_questions: list[str] = Field(default_factory=list, max_length=10)
    recommended_next_diagnostic_action: str | None = Field(default=None, max_length=500)

    def evidence_ids(self) -> set[uuid.UUID]:
        ids: set[uuid.UUID] = set()
        for statement in (self.incident_summary, self.impact, self.root_cause):
            ids.update(statement.evidence_ids)
        for service in self.affected_services:
            ids.update(service.evidence_ids)
        for entry in self.timeline:
            ids.update(entry.evidence_ids)
        for factor in self.contributing_factors:
            ids.update(factor.evidence_ids)
        ids.update(note.evidence_id for note in self.contradicting_evidence)
        return ids


class FinalInvestigationResult(_Strict):
    """Argument of `conclude_investigation`: the model's claim that it has
    sufficient evidence. Accepted only if the stopping criteria hold."""

    selected_hypothesis_key: str = Field(pattern=_KEY)
    confidence: float = Field(ge=0, le=1)
    rca: RcaDraft


class InconclusiveDeclaration(_Strict):
    """Argument of `declare_inconclusive`: cannot determine a root cause."""

    reason: str = Field(min_length=1, max_length=1000)
    evidence_gaps: list[str] = Field(min_length=1, max_length=10)
    leading_hypothesis_key: str | None = Field(default=None, pattern=_KEY)


# --- model turns and decisions ------------------------------------------------------

UPDATE_HYPOTHESES = "update_hypotheses"
CONCLUDE = "conclude_investigation"
DECLARE_INCONCLUSIVE = "declare_inconclusive"
DECISION_TOOLS = (UPDATE_HYPOTHESES, CONCLUDE, DECLARE_INCONCLUSIVE)


class ModelAction(BaseModel):
    """One tool call exactly as the model emitted it -- unvalidated."""

    model_config = ConfigDict(frozen=True)

    call_id: str
    name: str
    arguments: dict[str, Any]


class ToolRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    call_id: str
    tool: str
    arguments: dict[str, Any]


class HypothesisUpdateCall(BaseModel):
    model_config = ConfigDict(frozen=True)

    call_id: str
    batch: HypothesisUpdateBatch


class ConclusionCall(BaseModel):
    model_config = ConfigDict(frozen=True)

    call_id: str
    result: FinalInvestigationResult


class InconclusiveCall(BaseModel):
    model_config = ConfigDict(frozen=True)

    call_id: str
    declaration: InconclusiveDeclaration


class InvalidCall(BaseModel):
    model_config = ConfigDict(frozen=True)

    call_id: str
    name: str
    error: str


class InvestigationDecision(BaseModel):
    """A validated, provider-neutral reading of one model turn.

    `rationale` is the model's visible text (observations/interpretation);
    it is recorded, never parsed for facts. Domain state changes only via
    the typed calls below.
    """

    model_config = ConfigDict(frozen=True)

    rationale: str = ""
    hypothesis_updates: list[HypothesisUpdateCall] = Field(default_factory=list)
    tool_requests: list[ToolRequest] = Field(default_factory=list)
    conclusion: ConclusionCall | None = None
    inconclusive: InconclusiveCall | None = None
    invalid_calls: list[InvalidCall] = Field(default_factory=list)

    @property
    def has_actions(self) -> bool:
        return bool(
            self.hypothesis_updates
            or self.tool_requests
            or self.conclusion
            or self.inconclusive
            or self.invalid_calls
        )

    @model_validator(mode="after")
    def _one_terminal(self) -> InvestigationDecision:
        if self.conclusion is not None and self.inconclusive is not None:
            raise ValueError("a turn cannot both conclude and declare inconclusive")
        return self


def _error_text(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}"
        for e in exc.errors(include_input=False, include_url=False)
    )[:1500]


def interpret_turn(
    text: str, actions: list[ModelAction], evidence_tool_names: frozenset[str]
) -> InvestigationDecision:
    """Validate raw model actions into a decision. Nothing invalid is
    dropped silently: it becomes an `InvalidCall` the model is told about."""
    updates: list[HypothesisUpdateCall] = []
    tools: list[ToolRequest] = []
    conclusion: ConclusionCall | None = None
    inconclusive: InconclusiveCall | None = None
    invalid: list[InvalidCall] = []

    for action in actions:
        try:
            if action.name == UPDATE_HYPOTHESES:
                batch = HypothesisUpdateBatch.model_validate(action.arguments)
                updates.append(HypothesisUpdateCall(call_id=action.call_id, batch=batch))
            elif action.name == CONCLUDE:
                result = FinalInvestigationResult.model_validate(action.arguments)
                if conclusion is not None or inconclusive is not None:
                    raise ValueError("only one terminal call per turn")
                conclusion = ConclusionCall(call_id=action.call_id, result=result)
            elif action.name == DECLARE_INCONCLUSIVE:
                declaration = InconclusiveDeclaration.model_validate(action.arguments)
                if conclusion is not None or inconclusive is not None:
                    raise ValueError("only one terminal call per turn")
                inconclusive = InconclusiveCall(call_id=action.call_id, declaration=declaration)
            elif action.name in evidence_tool_names:
                tools.append(
                    ToolRequest(
                        call_id=action.call_id, tool=action.name, arguments=action.arguments
                    )
                )
            else:
                raise ValueError(f"unknown tool {action.name!r}")
        except ValidationError as exc:
            invalid.append(
                InvalidCall(call_id=action.call_id, name=action.name, error=_error_text(exc))
            )
        except ValueError as exc:
            invalid.append(InvalidCall(call_id=action.call_id, name=action.name, error=str(exc)))

    return InvestigationDecision(
        rationale=text[:4000],
        hypothesis_updates=updates,
        tool_requests=tools,
        conclusion=conclusion,
        inconclusive=inconclusive,
        invalid_calls=invalid,
    )


# --- hypothesis rules ---------------------------------------------------------------


@dataclass
class HypothesisSnapshot:
    key: str
    description: str
    status: HypothesisStatus
    confidence: float | None
    supporting: list[uuid.UUID] = field(default_factory=list)
    contradicting: list[uuid.UUID] = field(default_factory=list)
    missing_evidence: list[str] = field(default_factory=list)
    cause_category: str | None = None
    component: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "description": self.description,
            "cause_category": self.cause_category,
            "component": self.component,
            "status": self.status.value,
            "confidence": self.confidence,
            "supporting_evidence_ids": [str(e) for e in self.supporting],
            "contradicting_evidence_ids": [str(e) for e in self.contradicting],
            "missing_evidence": self.missing_evidence,
        }


def check_hypothesis_update(
    update: HypothesisUpdate,
    existing: HypothesisSnapshot | None,
    accessible_evidence: set[uuid.UUID],
) -> list[str]:
    """Why this update must be rejected (empty = acceptable). The whole
    update is quarantined on any problem -- never partially applied."""
    problems: list[str] = []
    cited = set(update.supporting_evidence_ids) | set(update.contradicting_evidence_ids)
    unknown = sorted(str(e) for e in cited - accessible_evidence)
    if unknown:
        problems.append(
            "evidence ids not returned to this investigation by any tool: " + ", ".join(unknown)
        )
    overlap = set(update.supporting_evidence_ids) & set(update.contradicting_evidence_ids)
    if overlap:
        problems.append("the same evidence cannot both support and contradict a hypothesis")
    if existing is None:
        if not update.description:
            problems.append("description is required when creating a hypothesis")
        if update.cause_category is None or update.component is None:
            problems.append("cause_category and component are required when creating a hypothesis")
    else:
        for name in ("cause_category", "component"):
            given, fixed = getattr(update, name), getattr(existing, name)
            if given is not None and fixed is not None and given != fixed:
                problems.append(f"{name} is fixed once set (it is {fixed!r})")
        if existing.status in (HypothesisStatus.REJECTED, HypothesisStatus.SELECTED):
            problems.append(f"hypothesis {update.key} is {existing.status.value} and cannot change")
    if update.status == "REJECTED":
        contradicting = set(update.contradicting_evidence_ids) | set(
            existing.contradicting if existing else []
        )
        if not contradicting:
            problems.append("rejecting a hypothesis requires contradicting evidence")
    if update.status == "SUPPORTED":
        supporting = set(update.supporting_evidence_ids) | set(
            existing.supporting if existing else []
        )
        if not supporting:
            problems.append("marking a hypothesis SUPPORTED requires supporting evidence")
    return problems


def evaluate_conclusion(
    result: FinalInvestigationResult,
    hypotheses: dict[str, HypothesisSnapshot],
    evidence_types: dict[uuid.UUID, str],
    criteria: StoppingCriteria,
) -> list[str]:
    """Deterministic stopping criteria (docs/architecture/15-investigation-engine.md).
    Returns the unmet criteria; empty means the conclusion may be accepted.
    Grounding (every cited id accessible) is checked separately, first."""
    unmet: list[str] = []
    selected = hypotheses.get(result.selected_hypothesis_key)
    if selected is None:
        return [f"hypothesis {result.selected_hypothesis_key!r} does not exist"]
    if selected.status not in (HypothesisStatus.SUPPORTED,):
        unmet.append(
            f"selected hypothesis must be SUPPORTED (it is {selected.status.value}); "
            "update it with supporting evidence first"
        )

    supporting = set(selected.supporting)
    if len(supporting) < criteria.min_supporting_evidence:
        unmet.append(
            f"selected hypothesis needs >= {criteria.min_supporting_evidence} supporting "
            f"evidence items (has {len(supporting)})"
        )
    types = {evidence_types[e] for e in supporting if e in evidence_types}
    if len(types) < criteria.min_supporting_evidence_types:
        unmet.append(
            f"supporting evidence must span >= {criteria.min_supporting_evidence_types} "
            f"evidence types (has {sorted(types)})"
        )
    contradicting = set(selected.contradicting)
    if contradicting and len(supporting) <= len(contradicting):
        unmet.append(
            "selected hypothesis has at least as much contradicting as supporting evidence"
        )
    acknowledged = {note.evidence_id for note in result.rca.contradicting_evidence}
    unaddressed = sorted(str(e) for e in contradicting - acknowledged)
    if unaddressed:
        unmet.append(
            "contradicting evidence on the selected hypothesis must be explained in "
            "rca.contradicting_evidence: " + ", ".join(unaddressed)
        )
    if not set(result.rca.root_cause.evidence_ids) & supporting:
        unmet.append("rca.root_cause must cite the selected hypothesis's supporting evidence")

    if len(hypotheses) < criteria.min_hypotheses_considered:
        unmet.append(
            f"consider >= {criteria.min_hypotheses_considered} competing hypotheses "
            f"(considered {len(hypotheses)})"
        )
    still_open = sorted(
        h.key
        for h in hypotheses.values()
        if h.key != selected.key
        and h.status in (HypothesisStatus.ACTIVE, HypothesisStatus.SUPPORTED)
    )
    if still_open:
        unmet.append(
            "competing hypotheses must be WEAKENED or REJECTED with evidence first: "
            + ", ".join(still_open)
        )
    if result.confidence < criteria.min_confidence:
        unmet.append(f"confidence {result.confidence} is below {criteria.min_confidence}")
    return unmet


def ungrounded_ids(cited: set[uuid.UUID], accessible: set[uuid.UUID]) -> list[str]:
    return sorted(str(e) for e in cited - accessible)


# --- views returned by incident-core ------------------------------------------------


class InvestigationView(BaseModel):
    model_config = ConfigDict(frozen=True, protected_namespaces=())

    id: uuid.UUID
    incident_id: uuid.UUID
    attempt_number: int
    status: InvestigationStatus
    model_provider: str
    model_name: str
    model_settings: dict[str, Any]
    budget: InvestigationBudget
    iteration_count: int
    tool_call_count: int
    evidence_count: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    last_action: str | None
    failure_reason: str | None
    escalation_reason: str | None
    inconclusive_reason: str | None
    selected_hypothesis_id: uuid.UUID | None
    final_result: dict[str, Any] | None
    created_at: Any
    started_at: Any | None
    completed_at: Any | None


class StepView(BaseModel):
    model_config = ConfigDict(frozen=True)

    sequence: int
    iteration: int
    kind: StepKind
    call_id: str | None
    payload: dict[str, Any]
    latency_ms: int | None
    created_at: Any


@dataclass
class InvestigationState:
    """Everything the engine needs to (re)build its position -- loaded from
    persisted state, never kept only in memory (resumability)."""

    investigation: InvestigationView
    steps: list[StepView]
    hypotheses: dict[str, HypothesisSnapshot]
    accessible_evidence: set[uuid.UUID]
    evidence_types: dict[uuid.UUID, str]


class HypothesisUpdateOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    applied: list[dict[str, Any]]
    rejected: list[dict[str, Any]]


class ConclusionOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    accepted: bool
    unmet_criteria: list[str] = Field(default_factory=list)
    rca_report_id: uuid.UUID | None = None


class StartedInvestigation(BaseModel):
    model_config = ConfigDict(frozen=True)

    investigation_id: uuid.UUID
    incident_id: uuid.UUID
    created: bool
