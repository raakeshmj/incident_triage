"""The investigation engine end to end below the event layer: real Postgres
(incident-core + evidence schemas), the real EvidenceService and tool layer
(canned telemetry backends, real Git, isolated Redis), and a deterministic
scripted model. No model API is called."""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy import select

from packages.agents.fake import FakeInvestigationModel, turn
from packages.agents.model import ModelError
from packages.domain.errors import LeaseLostError
from packages.domain.investigation import (
    InvestigationBudget,
    InvestigationStatus,
    StepKind,
)
from packages.incident.db.models import (
    EvidenceRefRow,
    IncidentRow,
    OutboxEventRow,
    RcaReportRow,
)
from packages.incident.investigations import InvestigationCoreService
from simulator.changes.registry import ChangeRegistry
from tests import investigation_support as support


@pytest.fixture
def env(core, session_factory, evidence_session_factory, test_redis):
    registry = ChangeRegistry(test_redis)
    registry.seed()
    registry.record_deployment(service="checkout-service", version="1.1.0-bad")
    investigations = InvestigationCoreService(session_factory)
    evidence = support.build_evidence_service(evidence_session_factory, core, test_redis)

    class Env:
        pass

    e = Env()
    e.core, e.investigations, e.evidence, e.session_factory = (
        core,
        investigations,
        evidence,
        session_factory,
    )
    e.redis = test_redis
    return e


def _run(env, script, *, budget=None, owner="worker-a", attempts=3):
    incident_id = support.open_incident(env.core)
    investigation_id = support.start(env.investigations, incident_id, budget)
    model = FakeInvestigationModel(script)
    engine = support.make_engine(
        env.investigations, env.core, env.evidence, model, owner=owner, attempts=attempts
    )
    status = engine.run(investigation_id)
    return incident_id, investigation_id, status, model


def _incident_status(env, incident_id) -> str:
    with env.session_factory() as session:
        return session.get(IncidentRow, incident_id).status


def _kinds(env, investigation_id):
    return [s.kind for s in env.investigations.load_state(investigation_id).steps]


# --- the happy path -----------------------------------------------------------------


def test_successful_investigation_produces_a_grounded_rca_and_rca_ready(env):
    incident_id, investigation_id, status, model = _run(env, support.happy_script())

    assert status == InvestigationStatus.COMPLETED
    assert _incident_status(env, incident_id) == "RCA_READY"
    state = env.investigations.load_state(investigation_id)
    inv = state.investigation
    assert inv.iteration_count == 3 and inv.tool_call_count == 3 and inv.evidence_count == 3
    assert inv.input_tokens == 3000 and inv.output_tokens == 600
    assert {k: h.status.value for k, h in state.hypotheses.items()} == {
        "H1": "SELECTED",
        "H2": "WEAKENED",
    }

    with env.session_factory() as session:
        rca = session.execute(
            select(RcaReportRow).where(RcaReportRow.investigation_id == investigation_id)
        ).scalar_one()
        # every evidence id the RCA cites is a registered ref for this investigation
        cited = {uuid.UUID(e) for e in rca.report["root_cause"]["evidence_ids"]}
        refs = (
            session.execute(select(EvidenceRefRow).where(EvidenceRefRow.id.in_(cited)))
            .scalars()
            .all()
        )
        assert {r.investigation_id for r in refs} == {investigation_id}
        events = [
            e.event_type
            for e in session.execute(
                select(OutboxEventRow).where(OutboxEventRow.correlation_id == incident_id)
            ).scalars()
        ]
    assert rca.report["supporting_evidence"] and rca.report["investigation_actions"]
    assert "Root cause (confidence 0.80)" in rca.summary
    assert {"InvestigationStarted", "InvestigationCompleted", "IncidentStatusChanged"} <= set(
        events
    )

    # the model never got an incident id, raw query power, or the answer
    first = model.requests[0]
    assert "incident_id" not in first.transcript[0].text  # type: ignore[union-attr]
    assert "1.1.0-bad" not in first.transcript[0].text  # type: ignore[union-attr]
    assert _kinds(env, investigation_id)[0] == StepKind.CONTEXT
    assert _kinds(env, investigation_id)[-1] == StepKind.OUTCOME


def test_trace_is_complete_and_replayable(env):
    _, investigation_id, _, model = _run(env, support.happy_script())
    trace = env.investigations.get_trace(investigation_id)
    kinds = [s["kind"] for s in trace["steps"]]
    assert kinds.count("model_turn") == 3 and kinds.count("tool_call") == 3
    tool_steps = [s for s in trace["steps"] if s["kind"] == "tool_call"]
    assert all(
        s["payload"]["arguments"] is not None and s["latency_ms"] is not None for s in tool_steps
    )
    assert all(s["payload"]["evidence_ids"] for s in tool_steps)
    context = trace["steps"][0]["payload"]
    assert context["system_prompt"] and context["tools"] and context["context"]["alerts"]
    assert trace["rca_report"]["report"]["root_cause_hypothesis"]["key"] == "H1"
    # rebuilding the transcript from the trace reproduces what the model was shown
    from packages.agents.engine import build_transcript

    state = env.investigations.load_state(investigation_id)
    replayed = build_transcript([s for s in state.steps if s.sequence < 16])
    assert replayed[0] == model.requests[0].transcript[0]


# --- grounding and hypothesis validation ----------------------------------------------


def test_invented_evidence_ids_are_quarantined_and_recorded(env):
    fake_id = str(uuid.uuid4())
    script = [
        turn(("get_metric_window", {"metric": "error_rate"})),
        turn(
            (
                "update_hypotheses",
                {
                    "updates": [
                        {
                            "key": "H1",
                            "description": "made up",
                            "status": "SUPPORTED",
                            "supporting_evidence_ids": [fake_id],
                            "rationale": "trust me",
                        }
                    ]
                },
            )
        ),
        turn(("declare_inconclusive", {"reason": "nothing firm", "evidence_gaps": ["no traces"]})),
    ]
    incident_id, investigation_id, status, model = _run(env, script)
    state = env.investigations.load_state(investigation_id)
    assert state.hypotheses == {}  # never persisted
    update_step = next(s for s in state.steps if s.kind == StepKind.HYPOTHESIS_UPDATE)
    assert update_step.payload["rejected"][0]["key"] == "H1"
    assert fake_id in update_step.payload["rejected"][0]["problems"][0]
    shown = model.requests[2].transcript[-1]
    assert shown.results[0].is_error  # type: ignore[union-attr]
    assert status == InvestigationStatus.ESCALATED


def test_rca_citing_unseen_evidence_is_rejected_and_the_run_continues(env):
    def bad_conclusion(request):
        return support.conclude_turn(request).__class__(
            **{
                **support.conclude_turn(request).__dict__,
                "actions": [
                    a.model_copy(
                        update={
                            "arguments": {
                                **a.arguments,
                                "rca": {
                                    **a.arguments["rca"],
                                    "impact": {"text": "x", "evidence_ids": [str(uuid.uuid4())]},
                                },
                            }
                        }
                    )
                    for a in support.conclude_turn(request).actions
                ],
            }
        )

    script = [support.GATHER, support.hypotheses_turn, bad_conclusion, support.conclude_turn]
    incident_id, investigation_id, status, _ = _run(env, script)
    kinds = _kinds(env, investigation_id)
    assert StepKind.CONCLUSION_REJECTED in kinds
    rejected = next(
        s
        for s in env.investigations.load_state(investigation_id).steps
        if s.kind == StepKind.CONCLUSION_REJECTED
    )
    assert "never shown" in rejected.payload["unmet_criteria"][0]
    assert status == InvestigationStatus.COMPLETED  # the corrected conclusion was accepted


def test_contradictory_hypotheses_block_the_conclusion_until_resolved(env):
    def both_supported(request):
        from packages.agents.fake import evidence_ids

        metric, deploy = evidence_ids(request, "metric")[0], evidence_ids(request, "deployment")[0]
        return turn(
            (
                "update_hypotheses",
                {
                    "updates": [
                        {
                            "key": "H1",
                            "description": "deploy regression",
                            "status": "SUPPORTED",
                            "supporting_evidence_ids": [metric, deploy],
                            "rationale": "r",
                        },
                        {
                            "key": "H2",
                            "description": "payment dependency",
                            "status": "SUPPORTED",
                            "supporting_evidence_ids": [metric],
                            "rationale": "r",
                        },
                    ]
                },
            )
        )

    def weaken_h2(request):
        from packages.agents.fake import evidence_ids

        health = evidence_ids(request, "metric")[-1]
        return turn(
            (
                "update_hypotheses",
                {
                    "updates": [
                        {
                            "key": "H2",
                            "status": "REJECTED",
                            "contradicting_evidence_ids": [health],
                            "rationale": "payment healthy",
                        }
                    ]
                },
            )
        )

    script = [
        support.GATHER,
        both_supported,
        support.conclude_turn,  # rejected: H2 still SUPPORTED
        weaken_h2,
        support.conclude_turn,
    ]
    _, investigation_id, status, _ = _run(env, script)
    state = env.investigations.load_state(investigation_id)
    rejected = [s for s in state.steps if s.kind == StepKind.CONCLUSION_REJECTED]
    assert any("H2" in c for c in rejected[0].payload["unmet_criteria"])
    assert status == InvestigationStatus.COMPLETED
    assert state.hypotheses["H2"].status.value == "REJECTED"


# --- outcomes without a root cause -----------------------------------------------------


def test_insufficient_evidence_escalates_and_never_reaches_rca_ready(env):
    script = [
        turn(("get_logs", {"severities": ["ERROR"]})),
        turn(
            (
                "declare_inconclusive",
                {"reason": "logs alone don't explain it", "evidence_gaps": ["no deploy data"]},
            )
        ),
    ]
    incident_id, investigation_id, status, _ = _run(env, script)
    inv = env.investigations.load_state(investigation_id).investigation
    assert status == InvestigationStatus.ESCALATED
    assert _incident_status(env, incident_id) == "ESCALATED"
    assert inv.inconclusive_reason == "logs alone don't explain it"
    assert inv.final_result["evidence_gaps"] == ["no deploy data"]
    with env.session_factory() as session:
        assert session.execute(select(RcaReportRow)).first() is None
        failed = session.execute(
            select(OutboxEventRow).where(OutboxEventRow.event_type == "InvestigationFailed")
        ).scalar_one()
    assert failed.payload["outcome"] == "ESCALATED"
    assert failed.payload["reason_code"] == "inconclusive"


def test_iteration_budget_gives_a_final_turn_then_escalates(env):
    script = [
        turn(("get_metric_window", {"metric": m}))
        for m in ("error_rate", "cpu_usage", "memory_usage")
    ]
    incident_id, investigation_id, status, model = _run(
        env, script, budget=InvestigationBudget(max_iterations=3)
    )
    assert status == InvestigationStatus.ESCALATED
    inv = env.investigations.load_state(investigation_id).investigation
    assert inv.final_result["reason_code"] == "budget_exhausted"
    assert inv.tool_call_count == 2  # the final turn's evidence call was refused
    final_notice = model.requests[-1].transcript[-1].notices[-1]  # type: ignore[union-attr]
    assert "FINAL TURN" in final_notice
    assert _incident_status(env, incident_id) == "ESCALATED"


def test_tool_call_budget_forces_a_final_turn(env):
    script = [
        turn(("get_metric_window", {"metric": "error_rate"}), ("get_logs", {})),
        turn(("declare_inconclusive", {"reason": "out of calls", "evidence_gaps": ["more data"]})),
    ]
    _, investigation_id, status, model = _run(
        env, script, budget=InvestigationBudget(max_tool_calls=2)
    )
    assert "FINAL TURN" in model.requests[1].transcript[-1].notices[-1]  # type: ignore[union-attr]
    assert status == InvestigationStatus.ESCALATED


def test_repeated_identical_tool_calls_are_refused(env):
    same = ("get_metric_window", {"metric": "error_rate"})
    script = [
        turn(same),
        turn(same),
        turn(("declare_inconclusive", {"reason": "r", "evidence_gaps": ["g"]})),
    ]
    _, investigation_id, _, model = _run(
        env, script, budget=InvestigationBudget(max_identical_tool_calls=1)
    )
    steps = [
        s
        for s in env.investigations.load_state(investigation_id).steps
        if s.kind == StepKind.TOOL_CALL
    ]
    assert [s.payload["ok"] for s in steps] == [True, False]
    assert steps[1].payload["error_code"] == "repeated_call"
    assert env.investigations.load_state(investigation_id).investigation.tool_call_count == 1


# --- failures ------------------------------------------------------------------------


def test_transient_model_failures_are_retried(env):
    script = [
        ModelError("RateLimitError", "429", retryable=True),
        ModelError("APITimeoutError", "timeout", retryable=True),
        *support.happy_script(),
    ]
    _, investigation_id, status, _ = _run(env, script)
    assert status == InvestigationStatus.COMPLETED
    errors = [
        s
        for s in env.investigations.load_state(investigation_id).steps
        if s.kind == StepKind.MODEL_ERROR
    ]
    assert [e.payload["code"] for e in errors] == ["RateLimitError", "APITimeoutError"]


def test_terminal_model_failure_fails_the_investigation(env):
    incident_id, investigation_id, status, _ = _run(
        env, [ModelError("AuthenticationError", "401", retryable=False)]
    )
    inv = env.investigations.load_state(investigation_id).investigation
    assert status == InvestigationStatus.FAILED
    assert inv.failure_reason.startswith("model_error")
    assert _incident_status(env, incident_id) == "ESCALATED"


def test_exhausted_retries_fail_as_model_unavailable(env):
    script = [ModelError("OverloadedError", "529", retryable=True)] * 2
    _, investigation_id, status, _ = _run(env, script, attempts=2)
    assert status == InvestigationStatus.FAILED
    assert env.investigations.load_state(investigation_id).investigation.failure_reason.startswith(
        "model_unavailable"
    )


def test_malformed_output_is_bounded(env):
    script = [
        turn(text="I believe it is the database."),  # no tool call
        turn(("update_hypotheses", {"updates": "not a list"})),  # schema-invalid
        turn(("launch_rollback", {"service": "checkout-service"})),  # not a tool
    ]
    incident_id, investigation_id, status, _ = _run(env, script)
    state = env.investigations.load_state(investigation_id)
    assert status == InvestigationStatus.FAILED
    assert state.investigation.failure_reason.startswith("malformed_output")
    assert [s.kind for s in state.steps].count(StepKind.INVALID_CALL) == 2
    assert state.hypotheses == {}


def test_evidence_service_outage_is_bounded(env, evidence_session_factory):
    env.evidence = support.build_evidence_service(
        evidence_session_factory, env.core, env.redis, loki=lambda r: httpx.Response(503)
    )
    script = [turn(("get_logs", {"severities": [s]})) for s in ("ERROR", "WARNING", "INFO")]
    _, investigation_id, status, _ = _run(
        env, script, budget=InvestigationBudget(max_consecutive_tool_failures=2)
    )
    inv = env.investigations.load_state(investigation_id).investigation
    assert status == InvestigationStatus.FAILED
    assert inv.failure_reason.startswith("evidence_unavailable")


# --- resumability and duplicate delivery -----------------------------------------------


def test_a_crashed_run_resumes_from_its_trace_not_from_zero(env):
    incident_id = support.open_incident(env.core)
    investigation_id = support.start(env.investigations, incident_id)

    crashing = FakeInvestigationModel([support.GATHER, RuntimeError("worker killed")])
    engine_a = support.make_engine(env.investigations, env.core, env.evidence, crashing, owner="a")
    with pytest.raises(RuntimeError):
        engine_a.run(investigation_id)
    before = env.investigations.load_state(investigation_id)
    assert before.investigation.iteration_count == 1
    assert before.investigation.status == InvestigationStatus.INVESTIGATING

    # a second worker can't take it while the lease is live...
    resumer = FakeInvestigationModel([support.hypotheses_turn, support.conclude_turn])
    engine_b = support.make_engine(env.investigations, env.core, env.evidence, resumer, owner="b")
    assert engine_b.run(investigation_id) is None
    # ...but once it lapses, it resumes where the trace left off
    env.investigations.release(investigation_id, owner="a")
    assert investigation_id in env.investigations.resumable()
    assert engine_b.run(investigation_id) == InvestigationStatus.COMPLETED

    after = env.investigations.load_state(investigation_id)
    assert [s.kind for s in after.steps].count(StepKind.CONTEXT) == 1
    assert after.investigation.iteration_count == 3
    # the resumed model was shown the earlier turn and its tool results
    assert len(resumer.requests[0].transcript) >= 4


def test_pending_tool_calls_from_a_persisted_turn_run_without_asking_the_model(env):
    incident_id = support.open_incident(env.core)
    investigation_id = support.start(env.investigations, incident_id)
    env.investigations.claim(investigation_id, owner="a", lease_seconds=120)
    # simulate a crash right after the model turn was persisted
    pending = support.GATHER
    env.investigations.record_step(
        investigation_id,
        owner="a",
        kind=StepKind.MODEL_TURN,
        iteration=1,
        payload={
            "text": pending.text,
            "actions": [a.model_dump() for a in pending.actions],
            "stop_reason": "tool_use",
            "usage": pending.usage,
            "served_model": "fake-model",
            "provider_payload": pending.provider_payload,
        },
    )
    env.investigations.release(investigation_id, owner="a")

    model = FakeInvestigationModel([support.hypotheses_turn, support.conclude_turn])
    engine = support.make_engine(env.investigations, env.core, env.evidence, model, owner="b")
    assert engine.run(investigation_id) == InvestigationStatus.COMPLETED
    tool_steps = [
        s
        for s in env.investigations.load_state(investigation_id).steps
        if s.kind == StepKind.TOOL_CALL
    ]
    assert {s.call_id for s in tool_steps} == {a.call_id for a in pending.actions}
    assert len(model.requests) == 2  # the pending turn was not re-asked


def test_duplicate_start_and_run_are_idempotent(env):
    incident_id = support.open_incident(env.core)
    first = env.investigations.request_investigation(
        incident_id,
        model_provider="fake",
        model_name="fake-model",
        model_settings=support.FAKE_SPEC.settings(),
        budget=InvestigationBudget(),
    )
    second = env.investigations.request_investigation(
        incident_id,
        model_provider="fake",
        model_name="fake-model",
        model_settings=support.FAKE_SPEC.settings(),
        budget=InvestigationBudget(),
    )
    assert first.created and not second.created
    assert first.investigation_id == second.investigation_id

    model = FakeInvestigationModel(support.happy_script())
    engine = support.make_engine(env.investigations, env.core, env.evidence, model)
    assert engine.run(first.investigation_id) == InvestigationStatus.COMPLETED
    assert engine.run(first.investigation_id) is None  # a redelivered event does nothing
    assert model.remaining == 0


def test_a_worker_that_lost_its_lease_cannot_write(env):
    incident_id = support.open_incident(env.core)
    investigation_id = support.start(env.investigations, incident_id)
    env.investigations.claim(investigation_id, owner="a", lease_seconds=120)
    env.investigations.release(investigation_id, owner="a")
    env.investigations.claim(investigation_id, owner="b", lease_seconds=120)
    with pytest.raises(LeaseLostError):
        env.investigations.record_step(
            investigation_id, owner="a", kind=StepKind.FEEDBACK, iteration=1, payload={"text": "x"}
        )


def test_investigation_requires_triaging_with_a_firing_alert(env):
    from packages.domain.enums import AlertStatus
    from packages.domain.errors import InvalidIncidentTransitionError
    from tests.factories import make_alert_command

    result = env.core.handle_alert_received(make_alert_command(external_id="ep-x"))
    env.core.handle_alert_received(
        make_alert_command(external_id="ep-x", status=AlertStatus.RESOLVED)
    )
    with pytest.raises(InvalidIncidentTransitionError):
        support.start(env.investigations, result.incident_id)
