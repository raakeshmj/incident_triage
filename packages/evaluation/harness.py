"""The evaluation harness: scenario -> investigation -> recording -> grade.

    golden scenario --(ScenarioWorld: real evidence path, canned backends)-->
    real incident-core + real engine + configured model (fake or live)
      --> InvestigationRecording (investigation-traces/<id>.json)
      --> Grade                  (eval-results/<run-id>.json)
      --> aggregate over runs    (eval-results/<batch-id>-summary.json)

Runs against a dedicated evaluation database (EVAL_*_DATABASE_URL), which
is reset before every run: each run starts from an empty incident history,
so no run can see another's incidents or RCAs, and results are
reproducible. `EvalEnvironment.from_urls` refuses a database whose name
doesn't mark it as disposable.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import Engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from packages.agents.config import InvestigationSettings, ModelSpec
from packages.agents.engine import InvestigationEngine, RetryPolicy
from packages.agents.factory import ModelFactory
from packages.domain.commands import AlertReceivedCommand
from packages.domain.enums import AlertSeverity, AlertSource, AlertStatus
from packages.domain.investigation import InvestigationBudget, StepKind, StoppingCriteria
from packages.evaluation.grading import Grade, aggregate, grade
from packages.evaluation.heuristic import HeuristicInvestigator
from packages.evaluation.lifecycle import run_lifecycle
from packages.evaluation.recording import (
    TRACE_DIR,
    EvidenceStoreReader,
    InvestigationRecording,
    build_recording,
    save_recording,
)
from packages.evaluation.scenario import EVAL_CATALOG, Scenario, leaked_expectations
from packages.evaluation.world import ScenarioWorld
from packages.evidence.db import models as _evidence_models  # noqa: F401 (registers tables)
from packages.evidence.db.base import Base as EvidenceBase
from packages.evidence.db.base import make_engine as make_evidence_engine
from packages.evidence.db.base import make_session_factory as make_evidence_sessions
from packages.evidence.scope import ServiceCatalog
from packages.incident.db import models as _incident_models  # noqa: F401 (registers tables)
from packages.incident.db.base import Base as IncidentBase
from packages.incident.db.base import make_engine as make_incident_engine
from packages.incident.db.base import make_session_factory as make_incident_sessions
from packages.incident.investigations import InvestigationCoreService
from packages.incident.remediations import RemediationCoreService
from packages.incident.service import IncidentCoreService
from packages.incident.verifications import VerificationCoreService

RESULTS_DIR = Path("eval-results")
Mode = Literal["fake", "live"]

FAKE_SPEC = ModelSpec(
    provider="fake",
    model="heuristic-investigator",
    thinking="none",
    effort=None,
    max_tokens=4096,
    timeout_seconds=10,
    max_retries=0,
)


def reset_tables(incident_engine: Engine, evidence_engine: Engine) -> None:
    """Empty every incident-core and evidence table (each with its own role:
    neither role can touch the other's schema)."""
    for engine, base in ((incident_engine, IncidentBase), (evidence_engine, EvidenceBase)):
        tables = [f'{t.schema}."{t.name}"' for t in base.metadata.sorted_tables]
        if tables:
            with engine.begin() as conn:
                conn.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))


@dataclass
class EvalEnvironment:
    incident_sessions: Callable[[], Session]
    evidence_sessions: Callable[[], Session]
    reset: Callable[[], None]
    catalog: ServiceCatalog = field(default_factory=lambda: ServiceCatalog.load(EVAL_CATALOG))

    @classmethod
    def from_urls(cls, incident_url: str, evidence_url: str) -> EvalEnvironment:
        for url in (incident_url, evidence_url):
            database = make_url(url).database or ""
            if "eval" not in database and "test" not in database:
                raise ValueError(
                    f"refusing to evaluate against database {database!r}: evaluation resets "
                    "its database before every run; point EVAL_INCIDENT_CORE_DATABASE_URL / "
                    "EVAL_EVIDENCE_DATABASE_URL at a disposable one "
                    "(e.g. incident_intelligence_eval)"
                )
        incident_engine = make_incident_engine(incident_url)
        evidence_engine = make_evidence_engine(evidence_url)
        return cls(
            incident_sessions=make_incident_sessions(incident_engine),
            evidence_sessions=make_evidence_sessions(evidence_engine),
            reset=lambda: reset_tables(incident_engine, evidence_engine),
        )


@dataclass
class RunResult:
    run_id: str
    scenario_id: str
    recording: InvestigationRecording
    grade: Grade
    errors: list[str]
    recording_path: Path | None = None
    result_path: Path | None = None

    def summary(self) -> dict[str, Any]:
        g, r = self.grade, self.recording
        return {
            "run_id": self.run_id,
            "scenario": self.scenario_id,
            "model": {"provider": r.model["provider"], "name": r.model["name"]},
            "mode": r.mode,
            "passed": g.passed,
            "failures": g.failures,
            "root_cause_result": g.root_cause["result"],
            "root_cause": g.root_cause,
            "escalation": g.escalation,
            "evidence_grounding": {
                "cited_ids_valid": g.evidence["cited_ids_valid"],
                "invalid_citation_attempts": g.evidence["invalid_citation_attempts"],
                "unsupported_claims": g.evidence["unsupported_claims"],
                "required_discovered": g.evidence["required_discovered"],
                "required_cited": g.evidence["required_cited"],
                "coverage": g.evidence["coverage"],
                "contradictions_addressed": g.evidence["contradictions_addressed"],
            },
            "hypothesis_result": {
                "count": g.hypotheses["count"],
                "alternatives_considered": g.hypotheses["alternatives_considered"],
                "alternatives_resolved": g.hypotheses["alternatives_resolved"],
                "selected_justified": g.hypotheses["selected_justified"],
                "expected_competitors_considered": g.hypotheses["expected_competitors_considered"],
            },
            "tool_calls": g.process["tool_calls"],
            "iterations": g.process["iterations"],
            "tokens": {
                "input": g.process["input_tokens"],
                "output": g.process["output_tokens"],
                "cache_read": g.process["cache_read_tokens"],
                "cache_write": g.process["cache_write_tokens"],
            },
            "latency_ms": {"wall": g.process["wall_ms"], "model": g.process["model_latency_ms"]},
            "final_state": g.state["final_incident_status"],
            "lifecycle": g.lifecycle,
            "errors": self.errors,
            "recording": str(self.recording_path) if self.recording_path else None,
            "grade": g.model_dump(mode="json"),
        }


def open_incident(core: IncidentCoreService, scenario: Scenario) -> uuid.UUID:
    incident_id: uuid.UUID | None = None
    for i, alert in enumerate(scenario.alerts):
        labels = {
            "service": scenario.service,
            "environment": scenario.environment,
            "region": scenario.region,
            "alertname": alert.alertname,
        }
        if alert.alert_type:
            labels["alert_type"] = alert.alert_type
        result = core.handle_alert_received(
            AlertReceivedCommand(
                idempotency_key=f"eval:{uuid.uuid4()}",
                source=AlertSource.PROMETHEUS,
                external_id=f"{scenario.id}:{i}:{uuid.uuid4().hex[:8]}",
                labels=labels,
                annotations={"summary": alert.summary, "description": alert.description},
                severity=AlertSeverity(alert.severity),
                status=AlertStatus.FIRING,
                raw_payload={"labels": labels, "source": "evaluation"},
            )
        )
        incident_id = incident_id or result.incident_id
    assert incident_id is not None
    return incident_id


def _model_inputs(recording: InvestigationRecording) -> list[str]:
    """Everything the application sent the model (not what it wrote)."""
    inputs = [recording.prompt.get("system_prompt") or "", json.dumps(recording.context)]
    for step in recording.steps:
        if step["kind"] in (StepKind.MODEL_TURN.value, StepKind.MODEL_ERROR.value):
            continue
        inputs.append(json.dumps(step["payload"], default=str))
    return inputs


def run_scenario(
    scenario: Scenario,
    env: EvalEnvironment,
    *,
    mode: Mode,
    spec: ModelSpec,
    model_factory: ModelFactory,
    run_id: str | None = None,
    base_budget: InvestigationBudget | None = None,
    criteria: StoppingCriteria | None = None,
    traces_dir: Path | None = TRACE_DIR,
    results_dir: Path | None = RESULTS_DIR,
    retry: RetryPolicy | None = None,
    sleep: Callable[[float], None] = time.sleep,
    lifecycle: bool = True,
    verification_time_scale: float = 0.004,
) -> RunResult:
    run_id = run_id or f"{scenario.id}-{mode}-{uuid.uuid4().hex[:8]}"
    criteria = criteria or StoppingCriteria()
    env.reset()
    world = ScenarioWorld(scenario, env.catalog, datetime.now(UTC))
    core = IncidentCoreService(env.incident_sessions)
    investigations = InvestigationCoreService(env.incident_sessions, criteria=criteria)
    evidence = world.evidence_service(env.evidence_sessions, core)

    incident_id = open_incident(core, scenario)
    started = investigations.request_investigation(
        incident_id,
        model_provider=spec.provider,
        model_name=spec.model,
        model_settings=spec.settings(),
        budget=scenario.investigation_budget(base_budget),
    )
    engine = InvestigationEngine(
        gateway=investigations,
        incidents=core,
        evidence=evidence,
        catalog=env.catalog,
        model_factory=model_factory,
        owner=f"eval:{run_id}",
        criteria=criteria,
        retry=retry or RetryPolicy(attempts=3, backoff_seconds=0 if mode == "fake" else 2.0),
        tool_retry_backoff_seconds=0,
        sleep=sleep,
    )
    errors: list[str] = []
    t0 = time.monotonic()
    try:
        engine.run(started.investigation_id)
    except Exception as exc:  # the run is still recorded and graded
        errors.append(f"{type(exc).__name__}: {str(exc)[:300]}")
    remediations = RemediationCoreService(
        env.incident_sessions,
        topology=env.catalog,
        verification_time_scale=verification_time_scale,
    )
    verifications = VerificationCoreService(env.incident_sessions)
    lifecycle_extra: dict[str, Any] = {}
    if lifecycle and scenario.lifecycle is not None and not errors:
        try:
            lifecycle_extra = run_lifecycle(
                scenario,
                world=world,
                evidence=evidence,
                evidence_reader=EvidenceStoreReader(env.evidence_sessions),
                investigations=investigations,
                remediations=remediations,
                verifications=verifications,
                investigation_id=started.investigation_id,
                owner=f"eval:{run_id}",
                sleep=sleep,
            )
        except Exception as exc:  # recorded and graded, never hidden
            errors.append(f"lifecycle {type(exc).__name__}: {str(exc)[:300]}")
    wall_ms = int((time.monotonic() - t0) * 1000)
    recording = build_recording(
        started.investigation_id,
        investigations=investigations,
        incidents=core,
        evidence=evidence,
        model_mode=mode,
        evidence_mode="fixture",
        scenario_id=scenario.id,
        run_id=run_id,
        wall_ms=wall_ms,
        remediations=remediations if lifecycle and scenario.lifecycle else None,
        verifications=verifications if lifecycle and scenario.lifecycle else None,
        lifecycle_extra=lifecycle_extra,
    )
    leaked = leaked_expectations(scenario, _model_inputs(recording))
    if leaked:
        errors.append(f"grading key leaked into model context: {leaked}")
    result = RunResult(
        run_id=run_id,
        scenario_id=scenario.id,
        recording=recording,
        grade=grade(recording, scenario, criteria),
        errors=errors,
    )
    if errors:
        result.grade.passed = False
        result.grade.failures.extend(errors)
    if traces_dir is not None:
        result.recording_path = save_recording(recording, traces_dir)
    if results_dir is not None:
        results_dir.mkdir(parents=True, exist_ok=True)
        result.result_path = results_dir / f"{run_id}.json"
        result.result_path.write_text(json.dumps(result.summary(), indent=1, sort_keys=True))
    return result


def run_batch(
    scenarios: list[Scenario],
    env: EvalEnvironment,
    *,
    runs: int,
    mode: Mode,
    spec: ModelSpec,
    model_factory_for_run: Callable[[], ModelFactory],
    batch_id: str | None = None,
    results_dir: Path | None = RESULTS_DIR,
    traces_dir: Path | None = TRACE_DIR,
    on_result: Callable[[RunResult], None] | None = None,
    **kwargs: Any,
) -> tuple[list[RunResult], dict[str, Any]]:
    """`runs` runs of each scenario with one configured model, then one
    aggregate. (Repeatability, not model comparison.)"""
    batch_id = batch_id or f"batch-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"
    results: list[RunResult] = []
    for scenario in scenarios:
        for n in range(1, runs + 1):
            result = run_scenario(
                scenario,
                env,
                mode=mode,
                spec=spec,
                model_factory=model_factory_for_run(),
                run_id=f"{batch_id}-{scenario.id}-{n}",
                results_dir=results_dir,
                traces_dir=traces_dir,
                **kwargs,
            )
            results.append(result)
            if on_result is not None:
                on_result(result)
    summary = {
        "batch_id": batch_id,
        "mode": mode,
        "model": {"provider": spec.provider, "name": spec.model, "settings": spec.settings()},
        "runs_per_scenario": runs,
        "per_scenario": {
            s.id: aggregate([r.grade for r in results if r.scenario_id == s.id]) for s in scenarios
        },
        "overall": aggregate([r.grade for r in results]),
        "results": [str(r.result_path) for r in results if r.result_path],
    }
    if results_dir is not None:
        results_dir.mkdir(parents=True, exist_ok=True)
        (results_dir / f"{batch_id}-summary.json").write_text(
            json.dumps(summary, indent=1, sort_keys=True)
        )
    return results, summary


def fake_model_factory() -> ModelFactory:
    investigator = HeuristicInvestigator()
    return lambda spec: investigator


def live_spec(settings: InvestigationSettings) -> ModelSpec:
    from packages.agents.config import resolve_model_spec

    return resolve_model_spec(settings)
