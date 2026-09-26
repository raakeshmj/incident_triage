"""Phase 6 harness end to end on real Postgres: golden scenarios through the
real engine, incident-core and evidence service (canned backends), graded;
recordings replayed deterministically; grading of wrong, unsafe and broken
investigations. No model API, no telemetry backend."""

from __future__ import annotations

import copy
import json

import pytest
from sqlalchemy.engine import Engine

from packages.agents.config import ModelConfigError
from packages.agents.fake import FakeInvestigationModel, turn
from packages.agents.model import ModelTurn
from packages.evaluation import heuristic
from packages.evaluation.grading import aggregate, grade
from packages.evaluation.harness import (
    FAKE_SPEC,
    EvalEnvironment,
    reset_tables,
    run_batch,
    run_scenario,
)
from packages.evaluation.heuristic import Candidate, HeuristicInvestigator
from packages.evaluation.recording import (
    EvidenceStoreReader,
    InvestigationRecording,
    load_recording,
    save_recording,
)
from packages.evaluation.replay import render_timeline, signature, verify_replay
from packages.evaluation.scenario import load_scenarios
from packages.incident.investigations import InvestigationCoreService
from packages.incident.service import IncidentCoreService

SCENARIOS = load_scenarios()


@pytest.fixture
def env(engine: Engine, evidence_engine: Engine, session_factory, evidence_session_factory):
    return EvalEnvironment(
        incident_sessions=session_factory,
        evidence_sessions=evidence_session_factory,
        reset=lambda: reset_tables(engine, evidence_engine),
    )


def _run(env, scenario_id, model=None, **kwargs):
    investigator = model or HeuristicInvestigator()
    return run_scenario(
        SCENARIOS[scenario_id],
        env,
        mode="fake",
        spec=FAKE_SPEC,
        model_factory=lambda spec: investigator,
        traces_dir=None,
        results_dir=None,
        **kwargs,
    )


def _replay(env, recording: InvestigationRecording):
    env.reset()
    return verify_replay(
        recording,
        core=IncidentCoreService(env.incident_sessions),
        investigations=InvestigationCoreService(env.incident_sessions),
        evidence_reader=EvidenceStoreReader(env.evidence_sessions),
        catalog=env.catalog,
    )


# --- golden scenarios -------------------------------------------------------------


@pytest.mark.parametrize("scenario_id", list(SCENARIOS))
def test_golden_scenario_passes_with_the_fake_investigator(env, scenario_id):
    result = _run(env, scenario_id)
    g, scenario = result.grade, SCENARIOS[scenario_id]
    assert g.passed, g.failures
    assert result.errors == []  # includes the grading-key leak check
    assert g.state["investigation_outcome_status"] == scenario.expected.outcome
    # Phase 8: the rest of the loop, graded from recorded state
    assert scenario.lifecycle is not None and g.lifecycle is not None
    assert all(g.lifecycle["checks"].values()), g.lifecycle
    assert not any(g.lifecycle["unsafe"].values())
    assert g.state["final_incident_status"] == scenario.lifecycle.expected_final_state
    assert g.evidence["cited_ids_valid"] and not any(g.unsafe.values())
    if scenario.kind == "positive":
        assert g.root_cause["result"] == "correct"
        assert g.hypotheses["alternatives_resolved"] and g.hypotheses["selected_justified"]
    else:
        assert g.escalation["classification"] == "legitimate_escalation"
        assert g.escalation["correct"]


def test_the_grading_key_never_reaches_the_model(env):
    investigator = HeuristicInvestigator()
    _run(env, "cascading-failure", model=investigator)
    scenario = SCENARIOS["cascading-failure"]
    sent = [
        r.system_prompt + json.dumps([str(e) for e in r.transcript]) for r in investigator.requests
    ]
    for text in scenario.grading_key_texts():
        assert all(text not in s for s in sent)


# --- recordings and replay -----------------------------------------------------------


def test_recording_is_complete_and_round_trips(env, tmp_path):
    rec = _run(env, "bad-deployment").recording
    assert rec.mode == {"model": "fake", "evidence": "fixture"}
    assert rec.prompt["version"] == "investigation-v2" and rec.prompt["system_prompt"]
    assert rec.prompt["stable_prefix_digest"] and rec.context["incident"]["service"]
    assert rec.incident["alerts"] and rec.incident["final_status"] == "RESOLVED"
    assert [t["to"] for t in rec.incident_transitions] == [
        "INVESTIGATING",
        "RCA_READY",
        "AWAITING_APPROVAL",
        "REMEDIATION_IN_PROGRESS",
        "VERIFYING",
        "RESOLVED",
    ]
    assert rec.lifecycle is not None and rec.lifecycle["final_incident_status"] == "RESOLVED"
    verification = rec.lifecycle["verifications"][0]
    assert verification["verification"]["status"] == "PASSED"
    assert {link["role"] for link in verification["evidence"]} == {"baseline", "observation"}
    assert rec.tool_calls and all(t["arguments"] is not None for t in rec.tool_calls)
    shown = {e["evidence_id"] for e in rec.evidence if e["shown_to_investigation"]}
    assert {e for t in rec.tool_calls for e in t["evidence_ids"]} <= shown
    assert rec.hypothesis_transitions and rec.rca and rec.outcome["status"] == "COMPLETED"
    assert rec.totals["tool_calls"] == len(rec.tool_calls)
    assert all("cache" in t for t in rec.model_turns)
    path = save_recording(rec, tmp_path)
    assert load_recording(rec.recording_id, tmp_path) == rec
    assert "OUTCOME COMPLETED" in render_timeline(load_recording(path))


@pytest.mark.parametrize(
    "scenario_id",
    ["bad-deployment", "cascading-failure", "two-plausible-causes", "evidence-source-unavailable"],
)
def test_replay_reexecutes_deterministically(env, scenario_id):
    recording = _run(env, scenario_id).recording
    first = _replay(env, recording)
    assert first.deterministic, (first.differences, first.error)
    second = _replay(env, recording)
    assert second.deterministic
    assert signature(first.replayed) == signature(second.replayed) == signature(recording)  # type: ignore[arg-type]


def test_replay_detects_a_divergent_recording(env):
    recording = _run(env, "bad-deployment").recording
    tampered = recording.model_copy(deep=True)
    step = next(s for s in tampered.steps if s["kind"] == "model_turn")
    step["payload"]["actions"][0]["arguments"] = {"service": "orders-db"}  # not what was recorded
    report = _replay(env, tampered)
    assert not report.deterministic and "replay asked for" in (report.error or "")


def test_replay_recomputes_validation_rather_than_trusting_the_recording(env):
    recording = _run(env, "bad-deployment").recording
    tampered = recording.model_copy(deep=True)
    hyp = next(
        s
        for s in tampered.steps
        if s["kind"] == "model_turn" and s["payload"]["actions"][0]["name"] == "update_hypotheses"
    )
    update = hyp["payload"]["actions"][0]["arguments"]["updates"][0]
    update["supporting_evidence_ids"] = ["00000000-0000-0000-0000-00000000dead"]
    report = _replay(env, tampered)
    assert not report.deterministic
    assert report.replayed is not None
    assert any(
        "not returned to this investigation" in p
        for r in report.replayed.rejected_hypothesis_updates
        for p in r["problems"]
    )


# --- grading: incorrect, unsafe, ungrounded, malformed -------------------------------


class _StopsAtTheSymptom(HeuristicInvestigator):
    """Treats a failing dependency as the root cause even when that
    dependency's own deployment explains it."""

    def _candidates(self, service, observed):  # type: ignore[no-untyped-def]
        out = super()._candidates(service, observed)
        for c in out:
            if c.category == "deployment" and c.component != service:
                c.qualified = False
            if c.category == "dependency" and len(c.supporting) >= 2 and len(c.types) >= 2:
                c.qualified = True
        return out


def test_an_incorrect_root_cause_is_graded_incorrect(env):
    result = _run(env, "cascading-failure", model=_StopsAtTheSymptom())
    g = result.grade
    assert g.state["investigation_outcome_status"] == "RCA_READY"  # it met the criteria...
    assert g.root_cause["result"] == "incorrect"  # ...and is still wrong
    assert g.root_cause["component_match"] and not g.root_cause["category_match"]
    assert not g.passed


def test_rca_ready_when_escalation_was_expected_is_unsafe(env, monkeypatch):
    monkeypatch.setattr(heuristic, "CHANGE_LEAD", heuristic.timedelta(hours=2))  # ignores timing
    g = _run(env, "deployment-without-causal-evidence").grade
    assert g.state["investigation_outcome_status"] == "RCA_READY"
    assert g.unsafe["rca_when_escalation_expected"]
    assert g.root_cause["result"] == "incorrect" and not g.passed


def test_invented_evidence_ids_are_grounding_failures(env):
    def invent(request):  # type: ignore[no-untyped-def]
        real = HeuristicInvestigator().decide(request)
        for action in real.actions:
            for update in action.arguments.get("updates", []):
                update["supporting_evidence_ids"] = update["supporting_evidence_ids"] + [
                    "11111111-1111-1111-1111-111111111111"
                ]
        return real

    script = [HeuristicInvestigator().decide] * 2 + [invent] + [HeuristicInvestigator().decide] * 3
    g = _run(env, "bad-deployment", model=FakeInvestigationModel(script)).grade
    assert g.evidence["invalid_citation_attempts"] >= 1 and g.evidence["grounding_failure"]
    assert g.evidence["cited_ids_valid"]  # quarantined: nothing invalid was ever stored
    assert g.state["investigation_outcome_status"] != "RCA_READY" or g.evidence["cited_ids_valid"]


def test_an_unsupported_conclusion_is_rejected_and_counted(env):
    def premature(request):  # type: ignore[no-untyped-def]
        survey = HeuristicInvestigator().decide(request)  # turn 0: survey calls
        return survey

    def conclude_now(request):  # type: ignore[no-untyped-def]
        ids = [
            json.loads(r.content)["evidence_id"]
            for r in request.transcript[-1].results
            if not r.is_error
        ][:2]
        return turn(
            (
                "update_hypotheses",
                {
                    "updates": [
                        {
                            "key": "H1",
                            "description": "d",
                            "cause_category": "deployment",
                            "component": "checkout-service",
                            "status": "SUPPORTED",
                            "supporting_evidence_ids": ids,
                            "rationale": "r",
                        }
                    ]
                },
            ),
            (
                "conclude_investigation",
                {
                    "selected_hypothesis_key": "H1",
                    "confidence": 0.9,
                    "rca": {
                        "incident_summary": {"text": "t", "evidence_ids": ids[:1]},
                        "impact": {"text": "t", "evidence_ids": ids[:1]},
                        "affected_services": [
                            {"service": "checkout-service", "evidence_ids": ids[:1]}
                        ],
                        "timeline": [
                            {
                                "at": "2026-09-01T12:00:00+00:00",
                                "event": "e",
                                "evidence_ids": ids[:1],
                            }
                        ],
                        "root_cause": {"text": "t", "evidence_ids": ids},
                    },
                },
            ),
        )

    giving_up = turn(("declare_inconclusive", {"reason": "not enough", "evidence_gaps": ["more"]}))
    g = _run(
        env, "bad-deployment", model=FakeInvestigationModel([premature, conclude_now, giving_up])
    ).grade
    assert g.state["invalid_conclusions_rejected"] == 1  # one hypothesis, no competitors
    assert g.state["investigation_outcome_status"] == "ESCALATED"
    assert g.root_cause["result"] == "inconclusive" and not g.passed


def test_malformed_output_is_an_agent_failure_not_a_legitimate_escalation(env):
    junk = turn(("update_hypotheses", {"updates": "not a list"}))
    g = _run(env, "insufficient-evidence", model=FakeInvestigationModel([junk] * 5)).grade
    assert g.escalation["classification"] == "agent_failure"
    assert g.escalation["reason_code"] == "malformed_output"
    assert not g.escalation["correct"] and not g.passed  # right outcome, wrong reason


def test_a_missing_provider_credential_fails_the_investigation_cleanly(env):
    def no_credentials(spec):  # type: ignore[no-untyped-def]
        raise ModelConfigError("ANTHROPIC_API_KEY is not set")

    result = run_scenario(
        SCENARIOS["bad-deployment"],
        env,
        mode="live",
        spec=FAKE_SPEC,
        model_factory=no_credentials,
        traces_dir=None,
        results_dir=None,
    )
    assert result.errors == []  # no crash
    assert result.recording.outcome["status"] == "FAILED"
    assert result.recording.outcome["reason_code"] == "model_config_error"
    assert result.grade.escalation["classification"] == "agent_failure"


def test_a_provider_without_prompt_caching_works_and_says_so(env):
    rec = _run(env, "memory-pressure").recording  # the fake provider has no caching
    assert rec.totals["cache_requested"] is False and rec.totals["cache_supported"] is False
    assert all(t["cache"]["supported"] is False for t in rec.model_turns)
    assert rec.outcome["status"] == "COMPLETED"


def test_cache_metadata_from_a_caching_provider_is_recorded(env):
    base = HeuristicInvestigator()

    class Caching:
        provider, model_name = "fake", "caching-fake"

        def decide(self, request):  # type: ignore[no-untyped-def]
            t = base.decide(request)
            cache = {
                "requested": True,
                "supported": True,
                "strategy": "stable_prefix",
                "prefix_digest": request.stable_prefix_digest(),
                "read_tokens": 4000,
                "write_tokens": 0,
            }
            return ModelTurn(
                **{
                    **t.__dict__,
                    "usage": {
                        "input_tokens": 500,
                        "output_tokens": 50,
                        "cache_read_input_tokens": 4000,
                    },
                    "cache": cache,
                }
            )

    rec = _run(env, "memory-pressure", model=Caching()).recording
    assert rec.totals["cache_requested"] and rec.totals["cache_read_tokens"] == 4000 * len(
        rec.model_turns
    )
    digests = {t["cache"]["prefix_digest"] for t in rec.model_turns}
    assert digests == {rec.prompt["stable_prefix_digest"]}  # identical stable prefix every turn


# --- repeated runs and aggregation ------------------------------------------------------


def test_repeated_runs_aggregate_and_write_machine_readable_results(env, tmp_path):
    results, summary = run_batch(
        [SCENARIOS["bad-deployment"], SCENARIOS["two-plausible-causes"]],
        env,
        runs=2,
        mode="fake",
        spec=FAKE_SPEC,
        model_factory_for_run=lambda: lambda spec, i=HeuristicInvestigator(): i,
        results_dir=tmp_path / "results",
        traces_dir=tmp_path / "traces",
        batch_id="batch-test",
    )
    assert len(results) == 4
    overall = summary["overall"]
    assert overall["runs"] == 4 and overall["pass_rate"] == 1.0
    assert overall["root_cause_accuracy"] == 1.0 and overall["correct_escalation_rate"] == 1.0
    assert overall["escalation_rate"] == 0.5 and overall["evidence_grounding_failures"] == 0
    assert summary["per_scenario"]["bad-deployment"]["runs"] == 2
    one = json.loads((tmp_path / "results" / "batch-test-bad-deployment-1.json").read_text())
    for key in (
        "scenario",
        "model",
        "run_id",
        "root_cause_result",
        "evidence_grounding",
        "hypothesis_result",
        "tool_calls",
        "iterations",
        "tokens",
        "latency_ms",
        "final_state",
        "errors",
    ):
        assert key in one, key
    assert (tmp_path / "results" / "batch-test-summary.json").exists()
    assert len(list((tmp_path / "traces").glob("*.json"))) == 4
    # grading is a pure function of the recording: re-grading a saved trace agrees
    saved = load_recording(one["recording"])
    assert grade(saved, SCENARIOS["bad-deployment"]).passed
    assert aggregate([]) == {"runs": 0}


def test_recordings_never_contain_the_api_key(env, monkeypatch):
    secret = "sk-ant-api03-" + "Q" * 70
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)

    def leaky(request):  # type: ignore[no-untyped-def]
        t = HeuristicInvestigator().decide(request)
        return ModelTurn(**{**t.__dict__, "text": f"my key is {secret}"})

    rec = _run(env, "insufficient-evidence", model=FakeInvestigationModel([leaky] * 6)).recording
    dumped = json.dumps(rec.model_dump(mode="json"))
    assert (secret in dumped) is False and rec.redactions >= 1


def test_candidate_helper_is_deterministic():
    c = Candidate(key="k", category="deployment", component="c", description="d")
    c.supporting = {"a": "metric", "b": "log"}
    assert c.strength == (2, 2) and copy.deepcopy(c).strength == c.strength


def test_any_investigation_can_be_exported_from_the_main_database(env, tmp_path, capsys):
    from packages.evaluation import cli

    investigation_id = _run(env, "error-storm").recording.investigation_id
    assert cli._export(investigation_id, tmp_path) == 0
    exported = load_recording(str(investigation_id), tmp_path)
    assert exported.mode == {"model": "fake", "evidence": "live"}
    assert exported.outcome["status"] == "COMPLETED" and exported.tool_calls
    assert cli.replay_main(["--trace", str(tmp_path / f"{investigation_id}.json")]) == 0
    assert "OUTCOME COMPLETED" in capsys.readouterr().out


# --- Phase 8: lifecycle grading catches unsafe lifecycles -------------------------------


def _mutated(recording, **changes):
    data = copy.deepcopy(recording.model_dump(mode="json"))
    for path, value in changes.items():
        node = data
        keys = path.split(".")
        for key in keys[:-1]:
            node = node[int(key)] if key.isdigit() else node[key]
        node[keys[-1]] = value
    return type(recording).model_validate(data)


def test_lifecycle_grading_flags_resolution_without_verification(env):
    rec = _run(env, "bad-deployment").recording
    forged = _mutated(rec, **{"lifecycle.verifications": []})
    g = grade(forged, SCENARIOS["bad-deployment"])
    assert g.lifecycle["unsafe"]["resolved_without_passed_verification"]  # type: ignore[index]
    assert not g.passed


def test_lifecycle_grading_flags_execution_before_approval_and_duplicates(env):
    rec = _run(env, "bad-deployment").recording
    timeline = rec.lifecycle["remediations"][0]["timeline"]  # type: ignore[index]
    reordered = [t for t in timeline if t["event"] != "approved"]
    forged = _mutated(
        rec,
        **{
            "lifecycle.remediations.0.timeline": reordered,
            "lifecycle.executor_side_effects": 2,
        },
    )
    g = grade(forged, SCENARIOS["bad-deployment"])
    assert g.lifecycle["unsafe"]["execution_without_approval"]  # type: ignore[index]
    assert g.lifecycle["unsafe"]["duplicate_side_effects"]  # type: ignore[index]


def test_an_ineffective_remediation_fails_verification_and_does_not_resolve(env):
    result = _run(env, "ineffective-rollback")
    assert result.grade.passed, result.grade.failures
    lc = result.grade.lifecycle
    assert lc["executions_succeeded"] == 1 and lc["verification"] == "FAILED"  # type: ignore[index]
    assert lc["final_state"] == "VERIFICATION_FAILED"  # type: ignore[index]
    verification = result.recording.lifecycle["verifications"][0]["verification"]  # type: ignore[index]
    assert verification["next_action"] == "reinvestigate"


def test_lifecycle_recordings_replay_deterministically(env):
    recording = _run(env, "bad-deployment").recording
    report = _replay(env, recording)
    assert report.deterministic, (report.differences, report.error)
