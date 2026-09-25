"""The investigation engine: a bounded, resumable agent loop (ADR-0020).

    claim -> (context) -> [budget check -> model decides -> validate ->
    act through tools -> persist every step] -> COMPLETED | ESCALATED | FAILED

Division of responsibility:
- The model reasons: forms hypotheses, picks the next diagnostic step,
  interprets evidence, proposes a conclusion.
- The engine owns the loop: budgets, retries, validation of model output,
  tool dispatch through the read-only tool layer, and stopping.
- incident-core owns state: every step, hypothesis change and outcome is a
  command it validates and writes (stopping criteria and evidence-citation
  checks included). The engine keeps nothing that matters only in memory:
  each iteration starts by reloading persisted state, so a crash anywhere
  resumes from the last recorded step instead of from zero.
"""

from __future__ import annotations

import json
import random
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from packages.agents.config import ModelConfigError, ModelSpec
from packages.agents.context import build_context, render_context
from packages.agents.factory import ModelFactory
from packages.agents.model import (
    AssistantEntry,
    ContextEntry,
    DecisionRequest,
    ModelError,
    ModelTurn,
    ObservationEntry,
    ToolResultEntry,
    TranscriptEntry,
    stable_prefix_digest,
)
from packages.agents.prompts import system_prompt
from packages.agents.toolset import (
    EVIDENCE_TOOL_NAMES,
    GET_INCIDENT_EVIDENCE,
    InvestigationToolset,
    ToolOutcome,
)
from packages.domain.errors import LeaseLostError
from packages.domain.investigation import (
    ACTION_STEP_KINDS,
    TERMINAL_INVESTIGATION_STATUSES,
    ConclusionOutcome,
    FinalInvestigationResult,
    HypothesisUpdateBatch,
    HypothesisUpdateOutcome,
    InvestigationDecision,
    InvestigationState,
    InvestigationStatus,
    ModelAction,
    StepKind,
    StepView,
    StoppingCriteria,
    interpret_turn,
)
from packages.domain.views import EvidenceRefView, IncidentView
from packages.evidence.scope import ServiceCatalog
from packages.evidence.service import EvidenceService
from packages.telemetry.context import bind_context
from packages.telemetry.logging import get_logger
from packages.telemetry.metrics import get_metrics

log = get_logger(__name__)
metrics = get_metrics()

# Tool failures that mean the evidence path itself is down (vs. a bad query).
_BACKEND_FAILURES = frozenset({"backend_unavailable", "backend_timeout"})


class Toolset(Protocol):
    """What the engine needs from its tool surface. `InvestigationToolset`
    (live/fixture evidence) and the evaluation replay toolset implement it."""

    def execute(self, tool: str, arguments: dict[str, Any]) -> ToolOutcome: ...


# Built once per run from the loaded state (ids, budget, prior calls).
ToolsetFactory = Callable[[InvestigationState], Toolset]


class InvestigationGateway(Protocol):
    """incident-core's investigation commands (InvestigationCoreService)."""

    def claim(
        self, investigation_id: uuid.UUID, *, owner: str, lease_seconds: int
    ) -> InvestigationState | None: ...

    def load_state(self, investigation_id: uuid.UUID) -> InvestigationState: ...

    def record_step(
        self,
        investigation_id: uuid.UUID,
        *,
        owner: str,
        kind: StepKind,
        iteration: int,
        payload: dict[str, Any],
        call_id: str | None = None,
        latency_ms: int | None = None,
        usage: dict[str, int] | None = None,
        tool_call: bool = False,
        new_evidence: int = 0,
        last_action: str | None = None,
        lease_seconds: int = 120,
    ) -> int: ...

    def apply_hypothesis_updates(
        self,
        investigation_id: uuid.UUID,
        *,
        owner: str,
        iteration: int,
        call_id: str,
        batch: HypothesisUpdateBatch,
    ) -> HypothesisUpdateOutcome: ...

    def complete(
        self,
        investigation_id: uuid.UUID,
        *,
        owner: str,
        iteration: int,
        call_id: str,
        result: FinalInvestigationResult,
    ) -> ConclusionOutcome: ...

    def escalate(
        self,
        investigation_id: uuid.UUID,
        *,
        owner: str,
        iteration: int,
        outcome: InvestigationStatus,
        reason_code: str,
        detail: str,
        inconclusive_reason: str | None = None,
        evidence_gaps: list[str] | None = None,
        call_id: str | None = None,
    ) -> None: ...

    def release(self, investigation_id: uuid.UUID, *, owner: str) -> None: ...


class IncidentReader(Protocol):
    """incident-core's read API (IncidentCoreService)."""

    def get_incident_view(self, incident_id: uuid.UUID) -> IncidentView | None: ...

    def list_evidence_refs(self, incident_id: uuid.UUID) -> list[EvidenceRefView]: ...


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 3
    backoff_seconds: float = 2.0


class _Stop(Exception):
    """Internal: the investigation reached a terminal outcome."""

    def __init__(self, status: InvestigationStatus) -> None:
        self.status = status


class InvestigationEngine:
    def __init__(
        self,
        *,
        gateway: InvestigationGateway,
        incidents: IncidentReader,
        evidence: EvidenceService | None,
        catalog: ServiceCatalog,
        model_factory: ModelFactory,
        owner: str,
        criteria: StoppingCriteria | None = None,
        lease_seconds: int = 180,
        retry: RetryPolicy | None = None,
        tool_retry_backoff_seconds: float = 0.5,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
        toolset_factory: ToolsetFactory | None = None,
    ) -> None:
        self._gateway = gateway
        self._incidents = incidents
        self._evidence = evidence
        self._catalog = catalog
        self._model_factory = model_factory
        self._owner = owner
        self._criteria = criteria or StoppingCriteria()
        self._lease_seconds = lease_seconds
        self._retry = retry or RetryPolicy()
        self._tool_backoff = tool_retry_backoff_seconds
        self._clock = clock
        self._sleep = sleep
        self._toolset_factory = toolset_factory

    # --- entry point -----------------------------------------------------------

    def run(self, investigation_id: uuid.UUID) -> InvestigationStatus | None:
        """Run (or resume) an investigation to a terminal outcome. Returns
        None if another worker holds it or it had already finished."""
        state = self._gateway.claim(
            investigation_id, owner=self._owner, lease_seconds=self._lease_seconds
        )
        if state is None:
            log.info("investigation.not_claimed", investigation_id=str(investigation_id))
            return None
        inv = state.investigation
        with bind_context(incident_id=str(inv.incident_id)):
            try:
                return self._run(state)
            except _Stop as stop:
                return stop.status
            except LeaseLostError:
                metrics.increment("investigation.lease_lost")
                log.warning("investigation.lease_lost", investigation_id=str(investigation_id))
                return None

    def _run(self, state: InvestigationState) -> InvestigationStatus:
        inv = state.investigation
        spec = ModelSpec.from_persisted(inv.model_provider, inv.model_name, inv.model_settings)
        try:
            model = self._model_factory(spec)
        except ModelConfigError as exc:
            # e.g. no credentials for the configured provider: a visible,
            # terminal outcome for this investigation -- not a worker crash.
            self._escalate(
                state,
                inv.iteration_count,
                InvestigationStatus.FAILED,
                "model_config_error",
                str(exc)[:300],
            )
        if not any(s.kind == StepKind.CONTEXT for s in state.steps):
            self._record_context(state)
            state = self._gateway.load_state(inv.id)
        toolset = (
            self._toolset_factory(state)
            if self._toolset_factory is not None
            else self._live_toolset(state)
        )
        system = system_prompt(self._criteria)
        definitions = InvestigationToolset.definitions()

        while True:
            state = self._gateway.load_state(inv.id)
            if state.investigation.status in TERMINAL_INVESTIGATION_STATUSES:
                return state.investigation.status
            if self._resolve_pending(state, toolset):
                continue  # pending actions were processed: reload
            self._enforce_limits(state)

            iteration = state.investigation.iteration_count + 1
            final_reason = self._final_turn_reason(state, iteration)
            self._record(
                state,
                StepKind.FEEDBACK,
                iteration,
                {"text": _budget_notice(state, iteration, final_reason), "reason": "budget"},
            )
            state = self._gateway.load_state(inv.id)
            request = DecisionRequest(
                system_prompt=system, tools=definitions, transcript=build_transcript(state.steps)
            )
            turn = self._call_model(state, model, request, iteration)
            self._record(
                state,
                StepKind.MODEL_TURN,
                iteration,
                {
                    "text": turn.text,
                    "actions": [a.model_dump() for a in turn.actions],
                    "stop_reason": turn.stop_reason,
                    "usage": turn.usage,
                    "served_model": turn.served_model,
                    "provider_payload": turn.provider_payload,
                    "cache": turn.cache,
                },
                latency_ms=turn.latency_ms,
                usage=turn.usage,
                last_action=f"model turn {iteration}: {len(turn.actions)} tool call(s)",
            )
            metrics.observe(
                "investigation.model_latency_ms",
                turn.latency_ms,
                model=turn.served_model,
                iteration=iteration,
            )
            log.info(
                "investigation.model_turn",
                investigation_id=str(inv.id),
                iteration=iteration,
                model=turn.served_model,
                stop_reason=turn.stop_reason,
                actions=[a.name for a in turn.actions],
                latency_ms=turn.latency_ms,
                **{f"tokens_{k}": v for k, v in turn.usage.items()},
            )
            decision = interpret_turn(turn.text, turn.actions, EVIDENCE_TOOL_NAMES)
            self._process(state, decision, iteration, final_reason, toolset, turn.stop_reason)
            if final_reason is not None:
                self._escalate(
                    state,
                    iteration,
                    InvestigationStatus.ESCALATED,
                    "budget_exhausted",
                    f"{final_reason}; the final turn did not reach a conclusion",
                )

    # --- one model turn ------------------------------------------------------

    def _call_model(
        self,
        state: InvestigationState,
        model: Any,
        request: DecisionRequest,
        iteration: int,
    ) -> ModelTurn:
        for attempt in range(1, self._retry.attempts + 1):
            try:
                return model.decide(request)
            except ModelError as exc:
                metrics.increment(
                    "investigation.model_error", code=exc.code, retryable=exc.retryable
                )
                self._record(
                    state,
                    StepKind.MODEL_ERROR,
                    iteration,
                    {
                        "code": exc.code,
                        "retryable": exc.retryable,
                        "attempt": attempt,
                        "message": str(exc)[:300],
                    },
                )
                if not exc.retryable or attempt == self._retry.attempts:
                    reason = "model_error" if not exc.retryable else "model_unavailable"
                    self._escalate(
                        state,
                        iteration,
                        InvestigationStatus.FAILED,
                        reason,
                        f"{exc.code} after {attempt} attempt(s)",
                    )
                delay = self._retry.backoff_seconds * (2 ** (attempt - 1))
                self._sleep(delay + random.uniform(0, delay / 4))
        raise AssertionError("unreachable")  # pragma: no cover

    def _process(
        self,
        state: InvestigationState,
        decision: InvestigationDecision,
        iteration: int,
        final_reason: str | None,
        toolset: Toolset,
        stop_reason: str = "tool_use",
    ) -> None:
        inv = state.investigation
        if not decision.has_actions:
            why = (
                "Your response was cut off by the output limit before any tool call completed."
                if stop_reason == "max_tokens"
                else "Your turn made no tool call."
            )
            self._record(
                state,
                StepKind.FEEDBACK,
                iteration,
                {
                    "text": why + " Act only through tools: gather evidence, update "
                    "hypotheses, conclude_investigation, or declare_inconclusive.",
                    "reason": "no_action",
                },
            )
            metrics.increment("investigation.invalid_turn", reason="no_action")
            return

        for call in decision.hypothesis_updates:
            update_outcome = self._gateway.apply_hypothesis_updates(
                inv.id,
                owner=self._owner,
                iteration=iteration,
                call_id=call.call_id,
                batch=call.batch,
            )
            log.info(
                "investigation.hypotheses_updated",
                investigation_id=str(inv.id),
                iteration=iteration,
                applied=[
                    (a["key"], a["from_status"], a["to_status"]) for a in update_outcome.applied
                ],
                rejected=[r["key"] for r in update_outcome.rejected],
            )

        for invalid in decision.invalid_calls:
            metrics.increment("investigation.invalid_call", tool=invalid.name)
            self._record(
                state,
                StepKind.INVALID_CALL,
                iteration,
                {
                    "tool": invalid.name,
                    "error": invalid.error,
                    "observation": f"Rejected: {invalid.error}",
                    "is_error": True,
                },
                call_id=invalid.call_id,
            )

        used_calls = inv.tool_call_count
        used_evidence = inv.evidence_count
        for request in decision.tool_requests:
            refusal = None
            if final_reason is not None:
                refusal = ("final_turn", "Final turn: evidence tools are disabled.")
            elif (
                request.tool != GET_INCIDENT_EVIDENCE
                and used_evidence >= inv.budget.max_evidence_items
            ):
                refusal = ("evidence_budget_exhausted", "The evidence item budget is spent.")
            if refusal is not None:
                self._record(
                    state,
                    StepKind.TOOL_CALL,
                    iteration,
                    {
                        "tool": request.tool,
                        "arguments": request.arguments,
                        "ok": False,
                        "error_code": refusal[0],
                        "observation": f"Not executed: {refusal[1]}",
                        "is_error": True,
                        "evidence_ids": [],
                        "counted": False,
                    },
                    call_id=request.call_id,
                )
                continue
            outcome = toolset.execute(request.tool, request.arguments)
            counted = outcome.error_code not in ("budget_exceeded", "repeated_call")
            used_calls += 1 if counted else 0
            used_evidence += outcome.new_evidence
            self._record(
                state,
                StepKind.TOOL_CALL,
                iteration,
                {
                    "tool": request.tool,
                    "arguments": request.arguments,
                    "ok": outcome.ok,
                    "error_code": outcome.error_code,
                    "summary": outcome.summary,
                    "evidence_ids": outcome.evidence_ids,
                    "observation": outcome.observation,
                    "is_error": not outcome.ok,
                    "counted": counted,
                },
                call_id=request.call_id,
                latency_ms=outcome.latency_ms,
                tool_call=counted,
                new_evidence=outcome.new_evidence,
                last_action=f"{request.tool} -> {'ok' if outcome.ok else outcome.error_code}",
            )
            metrics.observe(
                "investigation.tool_latency_ms",
                outcome.latency_ms,
                tool=request.tool,
                ok=outcome.ok,
            )
            log.info(
                "investigation.tool_call",
                investigation_id=str(inv.id),
                iteration=iteration,
                tool=request.tool,
                ok=outcome.ok,
                error_code=outcome.error_code,
                evidence_ids=outcome.evidence_ids,
                latency_ms=outcome.latency_ms,
            )

        if decision.conclusion is not None:
            result = self._gateway.complete(
                inv.id,
                owner=self._owner,
                iteration=iteration,
                call_id=decision.conclusion.call_id,
                result=decision.conclusion.result,
            )
            if result.accepted:
                raise _Stop(InvestigationStatus.COMPLETED)
            log.info(
                "investigation.conclusion_rejected",
                investigation_id=str(inv.id),
                unmet=result.unmet_criteria,
            )
        if decision.inconclusive is not None:
            declaration = decision.inconclusive.declaration
            self._escalate(
                state,
                iteration,
                InvestigationStatus.ESCALATED,
                "inconclusive",
                declaration.reason,
                inconclusive_reason=declaration.reason,
                evidence_gaps=declaration.evidence_gaps,
                call_id=decision.inconclusive.call_id,
            )

    # --- resumability ------------------------------------------------------------

    def _resolve_pending(self, state: InvestigationState, toolset: Toolset) -> bool:
        """After a crash between a model turn and its tool results, act on
        the unanswered calls from the persisted turn (never re-asking the
        model). Returns True if anything was processed."""
        turns = [s for s in state.steps if s.kind == StepKind.MODEL_TURN]
        if not turns:
            return False
        last = turns[-1]
        after = [s for s in state.steps if s.sequence > last.sequence]
        answered = {s.call_id for s in after if s.kind in ACTION_STEP_KINDS or s.call_id}
        actions = [ModelAction(**a) for a in last.payload.get("actions", [])]
        pending = [a for a in actions if a.call_id not in answered]
        if not actions:
            if any(s.kind == StepKind.FEEDBACK for s in after):
                return False
            self._process(
                state,
                InvestigationDecision(),
                last.iteration,
                None,
                toolset,
                last.payload.get("stop_reason", "end_turn"),
            )
            return True
        if not pending:
            return False
        metrics.increment("investigation.resumed_pending_actions", count=len(pending))
        log.info(
            "investigation.resuming_pending_actions",
            investigation_id=str(state.investigation.id),
            calls=[a.name for a in pending],
        )
        final_reason = self._final_turn_reason(state, last.iteration, pending=True)
        decision = interpret_turn(last.payload.get("text", ""), pending, EVIDENCE_TOOL_NAMES)
        self._process(state, decision, last.iteration, final_reason, toolset)
        if final_reason is not None:
            self._escalate(
                state,
                last.iteration,
                InvestigationStatus.ESCALATED,
                "budget_exhausted",
                f"{final_reason}; the final turn did not reach a conclusion",
            )
        return True

    # --- budgets -----------------------------------------------------------------

    def _enforce_limits(self, state: InvestigationState) -> None:
        inv = state.investigation
        budget = inv.budget
        iteration = inv.iteration_count
        if inv.started_at is not None:
            elapsed = (self._clock() - inv.started_at).total_seconds()
            if elapsed > budget.max_wall_clock_seconds:
                self._escalate(
                    state,
                    iteration,
                    InvestigationStatus.ESCALATED,
                    "budget_exhausted",
                    f"wall clock {int(elapsed)}s exceeded {budget.max_wall_clock_seconds}s",
                )
        total_tokens = (
            inv.input_tokens + inv.output_tokens + inv.cache_read_tokens + inv.cache_creation_tokens
        )
        if budget.max_total_tokens is not None and total_tokens >= budget.max_total_tokens:
            self._escalate(
                state,
                iteration,
                InvestigationStatus.ESCALATED,
                "budget_exhausted",
                f"token budget {budget.max_total_tokens} spent ({total_tokens})",
            )
        if iteration >= budget.max_iterations:
            self._escalate(
                state,
                iteration,
                InvestigationStatus.ESCALATED,
                "budget_exhausted",
                f"iteration budget {budget.max_iterations} spent",
            )
        if _trailing_invalid_turns(state.steps) >= budget.max_consecutive_invalid_turns:
            self._escalate(
                state,
                iteration,
                InvestigationStatus.FAILED,
                "malformed_output",
                f"{budget.max_consecutive_invalid_turns} consecutive turns without a valid action",
            )
        if _trailing_backend_failures(state.steps) >= budget.max_consecutive_tool_failures:
            self._escalate(
                state,
                iteration,
                InvestigationStatus.FAILED,
                "evidence_unavailable",
                f"{budget.max_consecutive_tool_failures} consecutive evidence-service failures",
            )

    @staticmethod
    def _final_turn_reason(
        state: InvestigationState, iteration: int, *, pending: bool = False
    ) -> str | None:
        inv = state.investigation
        budget = inv.budget
        if iteration >= budget.max_iterations:
            return f"turn budget {budget.max_iterations} reached"
        if pending:
            return None
        if inv.tool_call_count >= budget.max_tool_calls:
            return f"evidence tool call budget {budget.max_tool_calls} spent"
        if inv.evidence_count >= budget.max_evidence_items:
            return f"evidence item budget {budget.max_evidence_items} spent"
        return None

    def _live_toolset(self, state: InvestigationState) -> InvestigationToolset:
        inv = state.investigation
        if self._evidence is None:
            raise ValueError("an engine without an evidence service needs a toolset_factory")
        return InvestigationToolset(
            self._evidence,
            incident_id=inv.incident_id,
            investigation_id=inv.id,
            max_tool_calls=inv.budget.max_tool_calls,
            max_identical_calls=inv.budget.max_identical_tool_calls,
            prior_calls=_prior_calls(state.steps),
            retry_backoff_seconds=self._tool_backoff,
        )

    # --- persistence helpers -----------------------------------------------------

    def _record_context(self, state: InvestigationState) -> None:
        inv = state.investigation
        incident = self._incidents.get_incident_view(inv.incident_id)
        assert incident is not None
        refs = self._incidents.list_evidence_refs(inv.incident_id)
        context = build_context(incident, refs, self._catalog, inv.budget, self._clock())
        self._record(
            state,
            StepKind.CONTEXT,
            0,
            {
                "context": context,
                "evidence_ids": [str(r.id) for r in refs],
                "tools": [t.name for t in InvestigationToolset.definitions()],
                "system_prompt": system_prompt(self._criteria),
                "prompt_version": inv.model_settings.get("prompt_version"),
                "stable_prefix_digest": stable_prefix_digest(
                    system_prompt(self._criteria), InvestigationToolset.definitions()
                ),
                "model": {"provider": inv.model_provider, "name": inv.model_name},
            },
            last_action="context built",
        )

    def _record(
        self,
        state: InvestigationState,
        kind: StepKind,
        iteration: int,
        payload: dict[str, Any],
        **kwargs: Any,
    ) -> None:
        self._gateway.record_step(
            state.investigation.id,
            owner=self._owner,
            kind=kind,
            iteration=iteration,
            payload=payload,
            lease_seconds=self._lease_seconds,
            **kwargs,
        )

    def _escalate(
        self,
        state: InvestigationState,
        iteration: int,
        outcome: InvestigationStatus,
        reason_code: str,
        detail: str,
        **kwargs: Any,
    ) -> None:
        self._gateway.escalate(
            state.investigation.id,
            owner=self._owner,
            iteration=iteration,
            outcome=outcome,
            reason_code=reason_code,
            detail=detail,
            **kwargs,
        )
        log.info(
            "investigation.outcome",
            investigation_id=str(state.investigation.id),
            outcome=outcome.value,
            reason_code=reason_code,
            # an inconclusive reason is model-written text: it's in the trace, not the logs
            detail=None if reason_code == "inconclusive" else detail,
        )
        raise _Stop(outcome)


# --- pure helpers: transcript and streaks ----------------------------------------------


def observation_for(step: StepView) -> tuple[str, bool]:
    """What the model was told for an action step -- rebuilt identically on
    every replay, so the transcript is a pure function of the trace."""
    payload = step.payload
    if step.kind in (StepKind.TOOL_CALL, StepKind.INVALID_CALL):
        return payload["observation"], bool(payload.get("is_error"))
    if step.kind == StepKind.HYPOTHESIS_UPDATE:
        rejected = payload.get("rejected", [])
        text = "Hypothesis update result: " + _json(
            {
                "applied": [
                    {"key": a["key"], "from": a["from_status"], "to": a["to_status"]}
                    for a in payload.get("applied", [])
                ],
                "rejected": rejected,
                "hypotheses": [
                    {
                        "key": h["key"],
                        "status": h["status"],
                        "confidence": h["confidence"],
                        "supporting": len(h["supporting_evidence_ids"]),
                        "contradicting": len(h["contradicting_evidence_ids"]),
                    }
                    for h in payload.get("hypotheses", [])
                ],
            }
        )
        return text, bool(rejected)
    if step.kind == StepKind.CONCLUSION_REJECTED:
        return (
            "Conclusion not accepted. Unmet criteria: "
            + "; ".join(payload.get("unmet_criteria", [])),
            True,
        )
    return "", False


def build_transcript(steps: list[StepView]) -> list[TranscriptEntry]:
    entries: list[TranscriptEntry] = []
    results: list[ToolResultEntry] = []
    notices: list[str] = []

    def flush() -> None:
        if results or notices:
            entries.append(ObservationEntry(results=list(results), notices=list(notices)))
            results.clear()
            notices.clear()

    for step in steps:
        if step.kind == StepKind.CONTEXT:
            entries.append(ContextEntry(text=render_context(step.payload["context"])))
        elif step.kind == StepKind.MODEL_TURN:
            flush()
            entries.append(
                AssistantEntry(
                    text=step.payload.get("text", ""),
                    actions=[ModelAction(**a) for a in step.payload.get("actions", [])],
                    provider_payload=step.payload.get("provider_payload", {}),
                )
            )
        elif step.kind in ACTION_STEP_KINDS and step.call_id:
            content, is_error = observation_for(step)
            results.append(
                ToolResultEntry(call_id=step.call_id, content=content, is_error=is_error)
            )
        elif step.kind == StepKind.FEEDBACK:
            notices.append(step.payload["text"])
    flush()
    return entries


def _prior_calls(steps: list[StepView]) -> list[tuple[str, dict[str, Any]]]:
    return [
        (s.payload["tool"], s.payload.get("arguments", {}))
        for s in steps
        if s.kind == StepKind.TOOL_CALL and s.payload.get("counted")
    ]


def _trailing_invalid_turns(steps: list[StepView]) -> int:
    """Consecutive most-recent model turns that produced no valid action."""
    count = 0
    turns = [s for s in steps if s.kind == StepKind.MODEL_TURN]
    for turn in reversed(turns):
        answers = [s for s in steps if s.iteration == turn.iteration and s.call_id]
        valid = [s for s in answers if s.kind != StepKind.INVALID_CALL]
        if not turn.payload.get("actions") or not valid:
            count += 1
        else:
            break
    return count


def _trailing_backend_failures(steps: list[StepView]) -> int:
    count = 0
    for step in reversed([s for s in steps if s.kind == StepKind.TOOL_CALL]):
        if not step.payload.get("ok") and step.payload.get("error_code") in _BACKEND_FAILURES:
            count += 1
        else:
            break
    return count


def _budget_notice(state: InvestigationState, iteration: int, final_reason: str | None) -> str:
    inv = state.investigation
    budget = inv.budget
    text = (
        f"Budget: turn {iteration} of {budget.max_iterations}; evidence tool calls "
        f"{inv.tool_call_count}/{budget.max_tool_calls}; evidence items "
        f"{inv.evidence_count}/{budget.max_evidence_items}."
    )
    if final_reason is not None:
        text += (
            f" FINAL TURN ({final_reason}): evidence tools are disabled. Call "
            "conclude_investigation if the criteria are met; otherwise declare_inconclusive."
        )
    return text


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)
