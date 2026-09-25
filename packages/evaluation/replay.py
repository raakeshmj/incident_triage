"""Replay a recorded investigation -- no model call, no telemetry backend.

Two levels:

1. `render_timeline(recording)` -- inspection. A pure function of the
   recording file: context, every model decision, every tool call with its
   arguments, evidence ids and result, every hypothesis transition and
   rejection, the outcome and the RCA. Needs nothing but the JSON.

2. `verify_replay(recording, env)` -- re-execution. Runs the *current*
   engine and incident-core again, in the evaluation database, with the
   model replaced by `ReplayModel` (the recorded turns, errors included, in
   order) and the tool layer replaced by `ReplayToolset` (the recorded tool
   results, their evidence ids registered as refs so grounding checks run
   for real). Every budget, validation, hypothesis rule, stopping criterion
   and state transition is recomputed; the result's `signature()` must
   equal the recording's. A divergence -- the engine asking for a different
   tool, or deciding differently -- is reported, never papered over. This is
   what makes a recording a regression test for the deterministic half of
   the system.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from packages.agents.engine import InvestigationEngine, RetryPolicy
from packages.agents.model import DecisionRequest, ModelError, ModelTurn
from packages.agents.toolset import GET_INCIDENT_EVIDENCE, ToolOutcome
from packages.domain.commands import AlertReceivedCommand, RegisterEvidenceRefCommand
from packages.domain.enums import AlertSeverity, AlertSource, AlertStatus
from packages.domain.investigation import (
    InvestigationBudget,
    InvestigationState,
    ModelAction,
    StepKind,
    StoppingCriteria,
)
from packages.evaluation.recording import InvestigationRecording, build_recording
from packages.evidence.scope import ServiceCatalog
from packages.incident.investigations import InvestigationCoreService
from packages.incident.service import IncidentCoreService

# Engine-side refusals never reached the tool layer; they are recomputed.
_ENGINE_REFUSALS = frozenset({"final_turn", "evidence_budget_exhausted"})


class ReplayDivergence(Exception):
    """The re-executed investigation asked for something the recording
    doesn't contain at that point."""


class ReplayModel:
    """Returns the recorded model turns (and raises the recorded model
    errors) in their original order, whatever it is asked."""

    provider = "replay"

    def __init__(self, recording: InvestigationRecording) -> None:
        self.model_name = recording.model["name"]
        self._queue = [
            s
            for s in recording.steps
            if s["kind"] in (StepKind.MODEL_TURN.value, StepKind.MODEL_ERROR.value)
        ]
        self.requests: list[DecisionRequest] = []

    @property
    def remaining(self) -> int:
        return len(self._queue)

    def decide(self, request: DecisionRequest) -> ModelTurn:
        self.requests.append(request)
        if not self._queue:
            raise ReplayDivergence("the engine asked for a model turn the recording doesn't have")
        step = self._queue.pop(0)
        p = step["payload"]
        if step["kind"] == StepKind.MODEL_ERROR.value:
            raise ModelError(p["code"], p.get("message", ""), retryable=p["retryable"])
        return ModelTurn(
            text=p.get("text", ""),
            actions=[ModelAction(**a) for a in p.get("actions", [])],
            stop_reason=p.get("stop_reason", "tool_use"),
            usage=p.get("usage", {}),
            provider_payload=p.get("provider_payload", {}),
            served_model=p.get("served_model") or self.model_name,
            latency_ms=0,
            cache=p.get("cache") or {},
        )


class ReplayToolset:
    """Returns the recorded tool results in order, verifying each request
    matches, and registers the returned evidence ids with incident-core so
    citation checks against them are real."""

    def __init__(
        self,
        recording: InvestigationRecording,
        core: IncidentCoreService,
        incident_id: uuid.UUID,
        investigation_id: uuid.UUID,
    ) -> None:
        self._core = core
        self._incident_id = incident_id
        self._investigation_id = investigation_id
        self._evidence = recording.evidence_by_id()
        self._queue = [
            s
            for s in recording.steps
            if s["kind"] == StepKind.TOOL_CALL.value
            and s["payload"].get("error_code") not in _ENGINE_REFUSALS
        ]

    @property
    def remaining(self) -> int:
        return len(self._queue)

    def execute(self, tool: str, arguments: dict[str, Any]) -> ToolOutcome:
        if not self._queue:
            raise ReplayDivergence(f"{tool} requested, but the recording has no more tool results")
        step = self._queue.pop(0)
        p = step["payload"]
        if p["tool"] != tool or _canonical(p["arguments"]) != _canonical(arguments):
            raise ReplayDivergence(
                f"recorded {p['tool']}({_canonical(p['arguments'])}) at sequence "
                f"{step['sequence']}, replay asked for {tool}({_canonical(arguments)})"
            )
        evidence_ids = list(p.get("evidence_ids", []))
        new = 0
        if p.get("ok") and tool != GET_INCIDENT_EVIDENCE:
            for evidence_id in evidence_ids:
                self.register(evidence_id)
                new += 1
        return ToolOutcome(
            ok=bool(p.get("ok")),
            observation=p.get("observation", ""),
            evidence_ids=evidence_ids,
            new_evidence=new,
            error_code=p.get("error_code"),
            summary=p.get("summary"),
        )

    def register(self, evidence_id: str) -> None:
        record = self._evidence.get(evidence_id)
        if record is None:
            raise ReplayDivergence(f"evidence {evidence_id} is missing from the recording")
        self._core.register_evidence_ref(
            RegisterEvidenceRefCommand(
                evidence_id=uuid.UUID(evidence_id),
                incident_id=self._incident_id,
                investigation_id=self._investigation_id,
                evidence_type=record["evidence_type"],
                content_hash=record["content_hash"],
                source_system=record["source"],
                collected_at=record["collected_at"],
            )
        )


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


# --- signature: what must be identical for a replay to count as deterministic -------


def signature(recording: InvestigationRecording) -> dict[str, Any]:
    """Everything decided, not when or how fast: the ordered step kinds,
    each tool call (tool, args, result, evidence ids), hypothesis
    transitions and rejections, conclusion rejections, the outcome, the
    RCA's supporting evidence and the incident's final status."""
    steps = []
    for s in recording.steps:
        p = s["payload"]
        entry: list[Any] = [s["iteration"], s["kind"], s["call_id"]]
        if s["kind"] == StepKind.FEEDBACK.value:
            entry.append(p.get("text"))
        if s["kind"] == StepKind.INVALID_CALL.value:
            entry.append(p.get("error"))
        steps.append(entry)
    rca = recording.rca or {}
    return {
        "steps": steps,
        "tool_calls": [
            [t["tool"], _canonical(t["arguments"]), t["ok"], t["error_code"], t["evidence_ids"]]
            for t in recording.tool_calls
        ],
        "hypothesis_transitions": [
            [
                t["iteration"],
                t["key"],
                t["cause_category"],
                t["component"],
                t["from"],
                t["to"],
                sorted(t["added_supporting"]),
                sorted(t["added_contradicting"]),
            ]
            for t in recording.hypothesis_transitions
        ],
        "rejected_hypothesis_updates": [
            [r["iteration"], r["key"], r["problems"]] for r in recording.rejected_hypothesis_updates
        ],
        "conclusion_rejections": [
            [c["iteration"], c["unmet_criteria"]] for c in recording.conclusion_rejections
        ],
        "final_hypotheses": sorted(
            [h["key"], h["status"], sorted(h["supporting_evidence_ids"])]
            for h in recording.hypotheses
        ),
        "outcome": [recording.outcome["status"], recording.outcome["reason_code"]],
        "rca_supporting_evidence": sorted((rca.get("report") or {}).get("supporting_evidence", [])),
        "incident_final_status": recording.incident["final_status"],
        "incident_transitions": [[t["from"], t["to"]] for t in recording.incident_transitions],
    }


def diff_signatures(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    differences = []
    for key in expected:
        if expected[key] == actual.get(key):
            continue
        exp, act = expected[key], actual.get(key)
        if isinstance(exp, list) and isinstance(act, list):
            for i, (a, b) in enumerate(zip(exp, act, strict=False)):
                if a != b:
                    differences.append(f"{key}[{i}]: recorded {a!r}, replayed {b!r}")
                    break
            if len(exp) != len(act):
                differences.append(f"{key}: recorded {len(exp)} entries, replayed {len(act)}")
        else:
            differences.append(f"{key}: recorded {exp!r}, replayed {act!r}")
    return differences


# --- re-execution --------------------------------------------------------------------


@dataclass
class ReplayReport:
    deterministic: bool
    differences: list[str] = field(default_factory=list)
    error: str | None = None
    replayed: InvestigationRecording | None = None


def verify_replay(
    recording: InvestigationRecording,
    *,
    core: IncidentCoreService,
    investigations: InvestigationCoreService,
    evidence_reader: Any,
    catalog: ServiceCatalog,
    criteria: StoppingCriteria | None = None,
) -> ReplayReport:
    """Re-execute `recording` against a clean evaluation database (the caller
    resets it) and compare decisions. `evidence_reader` is only used to
    build the replayed recording and is never asked to query a backend."""
    incident_id: uuid.UUID | None = None
    for alert in recording.incident["alerts"]:
        result = core.handle_alert_received(
            AlertReceivedCommand(
                idempotency_key=f"replay:{recording.recording_id}:{uuid.uuid4()}",
                source=AlertSource(alert.get("source") or "prometheus"),
                external_id=None,
                labels=alert["labels"],
                annotations=alert["annotations"],
                severity=AlertSeverity(alert["severity"]),
                status=AlertStatus.FIRING,
                raw_payload={"replay_of": recording.recording_id},
            )
        )
        incident_id = incident_id or result.incident_id
    if incident_id is None:
        return ReplayReport(deterministic=False, error="the recording has no alerts")

    inv = recording.investigation
    started = investigations.request_investigation(
        incident_id,
        model_provider=inv["model_provider"],
        model_name=inv["model_name"],
        model_settings=inv["model_settings"],
        budget=InvestigationBudget(**inv["budget"]),
    )
    model = ReplayModel(recording)
    toolsets: list[ReplayToolset] = []

    def toolset_factory(state: InvestigationState) -> ReplayToolset:
        toolset = ReplayToolset(recording, core, incident_id, state.investigation.id)
        toolsets.append(toolset)
        return toolset

    # evidence the recorded context already listed must exist for the replay too
    seed = ReplayToolset(recording, core, incident_id, started.investigation_id)
    context_step = next((s for s in recording.steps if s["kind"] == StepKind.CONTEXT.value), None)
    for evidence_id in (context_step or {}).get("payload", {}).get("evidence_ids", []):
        seed.register(evidence_id)

    engine = InvestigationEngine(
        gateway=investigations,
        incidents=core,
        evidence=None,
        catalog=catalog,
        model_factory=lambda spec: model,
        owner=f"replay:{recording.recording_id}",
        criteria=criteria,
        retry=RetryPolicy(attempts=_attempts(recording), backoff_seconds=0),
        tool_retry_backoff_seconds=0,
        sleep=lambda _s: None,
        toolset_factory=toolset_factory,
    )
    t0 = time.monotonic()
    error: str | None = None
    try:
        engine.run(started.investigation_id)
    except ReplayDivergence as exc:
        error = str(exc)  # still recorded below, so the divergence can be inspected
    replayed = build_recording(
        started.investigation_id,
        investigations=investigations,
        incidents=core,
        evidence=evidence_reader,
        model_mode="replay",
        evidence_mode="replay",
        scenario_id=recording.scenario_id,
        run_id=recording.run_id,
        wall_ms=int((time.monotonic() - t0) * 1000),
    )
    used = {e for t in replayed.tool_calls for e in t["evidence_ids"]}
    replayed = replayed.model_copy(
        update={"evidence": [e for e in recording.evidence if e["evidence_id"] in used]}
    )
    differences = diff_signatures(signature(recording), signature(replayed))
    if model.remaining:
        differences.append(f"{model.remaining} recorded model turn(s) were never requested")
    leftover = sum(t.remaining for t in toolsets)
    if leftover:
        differences.append(f"{leftover} recorded tool result(s) were never requested")
    return ReplayReport(
        deterministic=not differences and error is None,
        differences=differences,
        error=error,
        replayed=replayed,
    )


def _attempts(recording: InvestigationRecording) -> int:
    errors_in_a_row, worst = 0, 0
    for s in recording.steps:
        if s["kind"] == StepKind.MODEL_ERROR.value:
            errors_in_a_row += 1
            worst = max(worst, errors_in_a_row)
        elif s["kind"] == StepKind.MODEL_TURN.value:
            errors_in_a_row = 0
    # the recorded run's retry limit is at least what it survived
    return max(3, worst + (0 if recording.outcome["status"] == "FAILED" else 1))


# --- inspection ------------------------------------------------------------------


def render_timeline(recording: InvestigationRecording) -> str:
    r = recording
    t = r.totals
    out = [
        f"investigation {r.recording_id}  [{r.mode['model']} model, {r.mode['evidence']} evidence]"
        + (f"  scenario={r.scenario_id}" if r.scenario_id else ""),
        f"model: {r.model['provider']}/{r.model['name']}  prompt={r.prompt.get('version')}  "
        f"prefix={str(r.prompt.get('stable_prefix_digest'))[:12]}",
        f"incident: {r.incident['service']} ({r.incident['environment']}, "
        f"{r.incident['severity']}) -> {r.incident['final_status']}",
        "alerts: " + ", ".join(a["labels"].get("alertname", "?") for a in r.incident["alerts"]),
        "",
    ]
    for s in r.steps:
        p, it = s["payload"], s["iteration"]
        kind = s["kind"]
        if kind == StepKind.CONTEXT.value:
            topo = r.context.get("service_topology", {})
            out.append(
                f"[{it}] context: {topo.get('service')} calls={topo.get('calls')} "
                f"called_by={topo.get('called_by')} "
                f"existing_evidence={len(p.get('evidence_ids', []))}"
            )
        elif kind == StepKind.MODEL_TURN.value:
            names = ", ".join(a["name"] for a in p.get("actions", [])) or "(no tool calls)"
            cache = p.get("cache") or {}
            cache_note = (
                f" cache r/w={cache.get('read_tokens', 0)}/{cache.get('write_tokens', 0)}"
                if cache.get("requested")
                else ""
            )
            out.append(f"[{it}] model: {names}{cache_note}")
            if p.get("text"):
                out.append(f"      says: {p['text'][:200]!r}")
        elif kind == StepKind.MODEL_ERROR.value:
            out.append(f"[{it}] model error: {p.get('code')} (retryable={p.get('retryable')})")
        elif kind == StepKind.TOOL_CALL.value:
            status = "ok" if p.get("ok") else p.get("error_code")
            out.append(
                f"[{it}]   {p['tool']}({_canonical(p.get('arguments', {}))}) -> {status} "
                f"{p.get('evidence_ids') or ''} {(p.get('summary') or '')[:120]}"
            )
        elif kind == StepKind.HYPOTHESIS_UPDATE.value:
            for a in p.get("applied", []):
                out.append(
                    f"[{it}]   hypothesis {a['key']} ({a.get('cause_category')}@"
                    f"{a.get('component')}): {a.get('from_status')} -> {a.get('to_status')} "
                    f"conf={a.get('confidence')} +sup={len(a.get('added_supporting', []))} "
                    f"+con={len(a.get('added_contradicting', []))}"
                )
            for rej in p.get("rejected", []):
                out.append(f"[{it}]   hypothesis {rej['key']} REJECTED UPDATE: {rej['problems']}")
        elif kind == StepKind.INVALID_CALL.value:
            out.append(f"[{it}]   invalid call {p.get('tool')}: {str(p.get('error'))[:160]}")
        elif kind == StepKind.CONCLUSION_REJECTED.value:
            out.append(f"[{it}]   conclusion rejected: {p.get('unmet_criteria')}")
        elif kind == StepKind.OUTCOME.value:
            out.append(f"[{it}] OUTCOME {p.get('status')} {p.get('reason_code') or ''}")
    out.append("")
    if r.rca:
        rc = r.rca["report"].get("root_cause_hypothesis", {})
        out.append(
            f"root cause: {rc.get('key')} {rc.get('cause_category')}@{rc.get('component')} "
            f"confidence={r.rca['report'].get('confidence')}"
        )
        out.append(r.rca["summary"])
    else:
        out.append(
            f"no RCA: {r.outcome['status']} {r.outcome['reason_code']} "
            f"{r.outcome.get('inconclusive_reason') or ''}"
        )
    out.append(
        f"totals: turns={t['model_turns']} tool_calls={t['tool_calls']} "
        f"evidence={t['evidence_items']} tokens in/out={t['input_tokens']}/{t['output_tokens']} "
        f"cache r/w={t['cache_read_tokens']}/{t['cache_write_tokens']} wall_ms={t['wall_ms']}"
    )
    return "\n".join(out)
