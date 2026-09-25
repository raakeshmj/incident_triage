"""Recorded investigations: the persisted trace as a first-class artifact.

incident-core already persists every step of an investigation (ADR-0020).
`build_recording` exports all of it -- plus the incident, its alerts and
status transitions, and the evidence records the tools returned -- as one
self-contained, versioned JSON document that can be inspected and replayed
without the database, the model, or any telemetry backend.

    live / fixture investigation  --record-->  InvestigationRecording (.json)
                                                  |-- replay.render_timeline (inspect)
                                                  |-- replay.verify_replay   (re-execute)
                                                  `-- grading.grade          (score)

Modes, as recorded in `mode`:
    model:    live (a real provider), fake (scripted/heuristic), replay
    evidence: live (real backends), fixture (a golden scenario), replay

Credentials never belong in a recording. Nothing that builds one reads them,
and `scrub_secrets` runs over the whole document before it is written:
known secret values from the environment/.env plus common credential
shapes are replaced with "[REDACTED]" and counted.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from packages.domain.investigation import StepKind
from packages.evidence import repository
from packages.evidence.models import EvidenceRecord
from packages.incident.investigations import InvestigationCoreService
from packages.incident.service import IncidentCoreService

SCHEMA_VERSION: Literal["recording-v1"] = "recording-v1"
REDACTED = "[REDACTED]"
_SECRET_SHAPES = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{12,}"),
    re.compile(r"postgres(?:ql)?(?:\+\w+)?://[^:@/\s]+:[^@/\s]+@"),
)
_SECRET_ENV = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD)$")


class EvidenceReader(Protocol):
    def get_incident_evidence(self, incident_id: uuid.UUID) -> list[EvidenceRecord]: ...


class EvidenceStoreReader:
    """Reads stored evidence records -- never a telemetry backend. Enough to
    export a recording of any investigation, live ones included."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def get_incident_evidence(self, incident_id: uuid.UUID) -> list[EvidenceRecord]:
        with self._session_factory() as session:
            return repository.list_incident_records(session, incident_id)


ModelMode = Literal["live", "fake", "replay"]
EvidenceMode = Literal["live", "fixture", "replay"]


class InvestigationRecording(BaseModel):
    """The recording document. Nested sections are plain JSON so a recording
    written by one version stays loadable by the next (`schema_version`)."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    schema_version: Literal["recording-v1"] = SCHEMA_VERSION
    recording_id: str
    recorded_at: str
    mode: dict[str, str]
    scenario_id: str | None = None
    run_id: str | None = None
    investigation: dict[str, Any]
    model: dict[str, Any]
    prompt: dict[str, Any]
    context: dict[str, Any]
    incident: dict[str, Any]
    incident_transitions: list[dict[str, Any]]
    steps: list[dict[str, Any]]
    model_turns: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    hypotheses: list[dict[str, Any]]
    hypothesis_transitions: list[dict[str, Any]]
    rejected_hypothesis_updates: list[dict[str, Any]]
    conclusion_rejections: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    rca: dict[str, Any] | None
    outcome: dict[str, Any]
    totals: dict[str, Any]
    redactions: int = 0

    @property
    def investigation_id(self) -> uuid.UUID:
        return uuid.UUID(self.recording_id)

    def evidence_by_id(self) -> dict[str, dict[str, Any]]:
        return {e["evidence_id"]: e for e in self.evidence}


def build_recording(
    investigation_id: uuid.UUID,
    *,
    investigations: InvestigationCoreService,
    incidents: IncidentCoreService,
    evidence: EvidenceReader,
    model_mode: ModelMode,
    evidence_mode: EvidenceMode,
    scenario_id: str | None = None,
    run_id: str | None = None,
    wall_ms: int | None = None,
    extra_secrets: list[str] | None = None,
) -> InvestigationRecording:
    state = investigations.load_state(investigation_id)
    trace = investigations.get_trace(investigation_id)
    inv = trace["investigation"]
    incident_id = uuid.UUID(inv["incident_id"])
    incident = incidents.get_incident_view(incident_id)
    steps: list[dict[str, Any]] = trace["steps"]

    context_step = next((s for s in steps if s["kind"] == StepKind.CONTEXT.value), None)
    context_payload = context_step["payload"] if context_step else {}
    model_turns = [_model_turn(s) for s in steps if s["kind"] == StepKind.MODEL_TURN.value]
    tool_calls = [_tool_call(s) for s in steps if s["kind"] == StepKind.TOOL_CALL.value]
    transitions, rejected = _hypothesis_history(steps)
    records = evidence.get_incident_evidence(incident_id)
    shown = {str(e) for e in state.accessible_evidence}
    evidence_out = [
        {
            "evidence_id": str(r.evidence_id),
            "evidence_type": r.evidence_type.value,
            "source": r.source_system.value,
            "operation": r.operation,
            "service": r.subject_service,
            "investigation_id": str(r.investigation_id) if r.investigation_id else None,
            "observed_at": r.observed_at.isoformat(),
            "window": [
                r.window_start.isoformat() if r.window_start else None,
                r.window_end.isoformat() if r.window_end else None,
            ],
            "collected_at": r.collected_at.isoformat(),
            "content_hash": r.content_hash,
            "summary": r.summary,
            "normalized_payload": r.normalized_payload,
            "shown_to_investigation": str(r.evidence_id) in shown,
        }
        for r in records
    ]
    outcome_step = next((s for s in reversed(steps) if s["kind"] == StepKind.OUTCOME.value), None)
    usage_cache = [t.get("cache") or {} for t in model_turns]
    recording = InvestigationRecording(
        recording_id=str(investigation_id),
        recorded_at=datetime.now(UTC).isoformat(),
        mode={"model": model_mode, "evidence": evidence_mode},
        scenario_id=scenario_id,
        run_id=run_id,
        investigation=inv,
        model={
            "provider": inv["model_provider"],
            "name": inv["model_name"],
            "settings": inv["model_settings"],
            "served_models": sorted({t["served_model"] for t in model_turns if t["served_model"]}),
        },
        prompt={
            "version": context_payload.get("prompt_version")
            or inv["model_settings"].get("prompt_version"),
            "system_prompt": context_payload.get("system_prompt"),
            "stable_prefix_digest": context_payload.get("stable_prefix_digest"),
            "tools": context_payload.get("tools", []),
        },
        context=context_payload.get("context", {}),
        incident={
            "id": str(incident_id),
            "service": incident.service if incident else None,
            "environment": incident.environment if incident else None,
            "severity": incident.severity if incident else None,
            "created_at": incident.created_at.isoformat() if incident else None,
            "final_status": incident.status if incident else None,
            "alerts": [
                {
                    "source": a.source,
                    "external_id": a.external_id,
                    "labels": a.labels,
                    "annotations": a.annotations,
                    "severity": a.severity,
                    "status": a.status,
                    "received_at": a.received_at.isoformat(),
                    "resolved_at": a.resolved_at.isoformat() if a.resolved_at else None,
                }
                for a in (incident.alerts if incident else [])
            ],
        },
        incident_transitions=investigations.incident_transitions(incident_id),
        steps=steps,
        model_turns=model_turns,
        tool_calls=tool_calls,
        hypotheses=trace["hypotheses"],
        hypothesis_transitions=transitions,
        rejected_hypothesis_updates=rejected,
        conclusion_rejections=[
            {"iteration": s["iteration"], "unmet_criteria": s["payload"].get("unmet_criteria", [])}
            for s in steps
            if s["kind"] == StepKind.CONCLUSION_REJECTED.value
        ],
        evidence=evidence_out,
        rca=trace["rca_report"],
        outcome={
            "status": inv["status"],
            "reason_code": (outcome_step or {}).get("payload", {}).get("reason_code"),
            "escalation_reason": inv["escalation_reason"],
            "failure_reason": inv["failure_reason"],
            "inconclusive_reason": inv["inconclusive_reason"],
            "final_result": inv["final_result"],
        },
        totals={
            "iterations": inv["iteration_count"],
            "model_turns": len(model_turns),
            "tool_calls": inv["tool_call_count"],
            "tool_call_attempts": len(tool_calls),
            "evidence_items": inv["evidence_count"],
            "input_tokens": inv["input_tokens"],
            "output_tokens": inv["output_tokens"],
            "cache_read_tokens": inv["cache_read_tokens"],
            "cache_write_tokens": inv["cache_creation_tokens"],
            "cache_requested": any(c.get("requested") for c in usage_cache),
            "cache_supported": any(c.get("supported") for c in usage_cache),
            "model_latency_ms": sum(t["latency_ms"] or 0 for t in model_turns),
            "tool_latency_ms": sum(t["latency_ms"] or 0 for t in tool_calls),
            "wall_ms": wall_ms if wall_ms is not None else _wall_ms(inv),
        },
    )
    return scrub_secrets(recording, extra_secrets)


def _model_turn(step: dict[str, Any]) -> dict[str, Any]:
    p = step["payload"]
    return {
        "sequence": step["sequence"],
        "iteration": step["iteration"],
        "text": p.get("text", ""),
        "actions": [{"call_id": a["call_id"], "name": a["name"]} for a in p.get("actions", [])],
        "stop_reason": p.get("stop_reason"),
        "usage": p.get("usage", {}),
        "cache": p.get("cache", {}),
        "served_model": p.get("served_model"),
        "latency_ms": step.get("latency_ms"),
    }


def _tool_call(step: dict[str, Any]) -> dict[str, Any]:
    p = step["payload"]
    return {
        "sequence": step["sequence"],
        "iteration": step["iteration"],
        "call_id": step["call_id"],
        "tool": p.get("tool"),
        "arguments": p.get("arguments", {}),
        "ok": p.get("ok"),
        "error_code": p.get("error_code"),
        "counted": p.get("counted"),
        "evidence_ids": p.get("evidence_ids", []),
        "summary": p.get("summary"),
        "latency_ms": step.get("latency_ms"),
    }


def _hypothesis_history(
    steps: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    transitions: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for step in steps:
        if step["kind"] != StepKind.HYPOTHESIS_UPDATE.value:
            continue
        for a in step["payload"].get("applied", []):
            transitions.append(
                {
                    "sequence": step["sequence"],
                    "iteration": step["iteration"],
                    "key": a["key"],
                    "cause_category": a.get("cause_category"),
                    "component": a.get("component"),
                    "from": a.get("from_status"),
                    "to": a.get("to_status"),
                    "confidence": a.get("confidence"),
                    "added_supporting": a.get("added_supporting", []),
                    "added_contradicting": a.get("added_contradicting", []),
                }
            )
        for r in step["payload"].get("rejected", []):
            rejected.append(
                {"iteration": step["iteration"], "key": r["key"], "problems": r["problems"]}
            )
    return transitions, rejected


def _wall_ms(inv: dict[str, Any]) -> int | None:
    started, completed = inv.get("started_at"), inv.get("completed_at")
    if not started or not completed:
        return None
    delta = datetime.fromisoformat(completed) - datetime.fromisoformat(started)
    return int(delta.total_seconds() * 1000)


# --- secrets -----------------------------------------------------------------------


def known_secret_values(extra: list[str] | None = None) -> list[str]:
    """Secret values this process could hold: credential-named env vars and
    .env entries (never printed -- only used to find and redact them)."""
    values = set(extra or [])
    candidates: list[tuple[str, str | None]] = list(os.environ.items())
    env_file = Path(".env")
    if env_file.exists():
        from dotenv import dotenv_values

        candidates += list(
            dotenv_values(env_file).items()
        )  # both sources, never one over the other
    for name, value in candidates:
        if value and _SECRET_ENV.search(name.upper()) and len(value) >= 8:
            values.add(value)
    return sorted(values, key=len, reverse=True)


def _scrub(value: Any, secrets: list[str], counter: list[int]) -> Any:
    if isinstance(value, str):
        out = value
        for secret in secrets:
            if secret in out:
                counter[0] += out.count(secret)
                out = out.replace(secret, REDACTED)
        for shape in _SECRET_SHAPES:
            out, n = shape.subn(REDACTED, out)
            counter[0] += n
        return out
    if isinstance(value, dict):
        return {k: _scrub(v, secrets, counter) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, secrets, counter) for v in value]
    return value


def scrub_secrets(
    recording: InvestigationRecording, extra: list[str] | None = None
) -> InvestigationRecording:
    counter = [0]
    data = _scrub(recording.model_dump(mode="json"), known_secret_values(extra), counter)
    data["redactions"] = recording.redactions + counter[0]
    return InvestigationRecording.model_validate(data)


# --- files -------------------------------------------------------------------------

TRACE_DIR = Path("investigation-traces")


def save_recording(recording: InvestigationRecording, directory: Path = TRACE_DIR) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{recording.recording_id}.json"
    path.write_text(json.dumps(recording.model_dump(mode="json"), indent=1, sort_keys=True))
    return path


def load_recording(ref: str | Path, directory: Path = TRACE_DIR) -> InvestigationRecording:
    """A path, or a recording id (file name without .json) under `directory`."""
    path = Path(ref)
    if not path.exists():
        path = directory / f"{ref}.json"
    data = json.loads(path.read_text())
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"{path}: not a {SCHEMA_VERSION} recording "
            f"(schema_version={data.get('schema_version')!r}); "
            "re-export it with `replay --export <investigation-id>`"
        )
    return InvestigationRecording.model_validate(data)
