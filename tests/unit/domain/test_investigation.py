"""Investigation domain rules: output validation, hypothesis lifecycle,
deterministic stopping criteria. Pure -- no model, no database."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from packages.domain.investigation import (
    FinalInvestigationResult,
    HypothesisSnapshot,
    HypothesisStatus,
    HypothesisUpdate,
    ModelAction,
    StoppingCriteria,
    check_hypothesis_update,
    evaluate_conclusion,
    interpret_turn,
)

M1, M2, D1, L1 = (uuid.uuid4() for _ in range(4))
TYPES = {M1: "metric", M2: "metric", D1: "deployment", L1: "log"}
TOOLS = frozenset({"get_logs", "get_metric_window"})


def _rca(root_ids, contradictions=()):
    statement = {"text": "x", "evidence_ids": [str(i) for i in root_ids]}
    return {
        "incident_summary": statement,
        "impact": statement,
        "affected_services": [{"service": "checkout-service", "evidence_ids": [str(M1)]}],
        "timeline": [
            {
                "at": datetime(2026, 9, 26, tzinfo=UTC).isoformat(),
                "event": "e",
                "evidence_ids": [str(D1)],
            }
        ],
        "root_cause": statement,
        "contradicting_evidence": [
            {"evidence_id": str(c), "explanation": "why it doesn't overturn"}
            for c in contradictions
        ],
    }


def _result(key="H1", root_ids=(M1, D1), confidence=0.8, contradictions=()):
    return FinalInvestigationResult.model_validate(
        {
            "selected_hypothesis_key": key,
            "confidence": confidence,
            "rca": _rca(root_ids, contradictions),
        }
    )


def _snap(key, status, supporting=(), contradicting=(), cause_category=None, component=None):
    return HypothesisSnapshot(
        key=key,
        description=key,
        status=HypothesisStatus(status),
        confidence=0.5,
        supporting=list(supporting),
        contradicting=list(contradicting),
        cause_category=cause_category,
        component=component,
    )


# --- interpret_turn -------------------------------------------------------------


def test_turn_actions_are_validated_into_a_typed_decision():
    decision = interpret_turn(
        "looking",
        [
            ModelAction(call_id="1", name="get_logs", arguments={"limit": 3}),
            ModelAction(
                call_id="2",
                name="update_hypotheses",
                arguments={"updates": [{"key": "H1", "description": "d", "rationale": "r"}]},
            ),
            ModelAction(call_id="3", name="run_shell", arguments={"cmd": "rm -rf /"}),
            ModelAction(call_id="4", name="update_hypotheses", arguments={"updates": "nope"}),
            ModelAction(
                call_id="5",
                name="update_hypotheses",
                arguments={"updates": [{"key": "H1", "rationale": "r", "incident_id": "x"}]},
            ),
        ],
        TOOLS,
    )
    assert [t.call_id for t in decision.tool_requests] == ["1"]
    assert [u.call_id for u in decision.hypothesis_updates] == ["2"]
    assert {c.call_id for c in decision.invalid_calls} == {"3", "4", "5"}
    unknown = next(c for c in decision.invalid_calls if c.call_id == "3")
    assert "unknown tool" in unknown.error
    extra = next(c for c in decision.invalid_calls if c.call_id == "5")
    assert "incident_id" in extra.error  # extra fields forbidden, never ignored


def test_a_turn_with_no_tool_calls_has_no_actions():
    assert not interpret_turn("I think it's the deploy.", [], TOOLS).has_actions


def test_malformed_conclusion_is_quarantined_not_accepted():
    decision = interpret_turn(
        "",
        [ModelAction(call_id="c", name="conclude_investigation", arguments={"confidence": 2})],
        TOOLS,
    )
    assert decision.conclusion is None and decision.invalid_calls[0].call_id == "c"


# --- hypothesis updates ---------------------------------------------------------


def _update(**kwargs):
    return HypothesisUpdate.model_validate({"key": "H1", "rationale": "r", **kwargs})


def test_updates_citing_unseen_evidence_are_rejected_whole():
    problems = check_hypothesis_update(
        _update(description="d", supporting_evidence_ids=[str(M1), str(uuid.uuid4())]),
        None,
        accessible_evidence={M1},
    )
    assert any("not returned to this investigation" in p for p in problems)


@pytest.mark.parametrize(
    ("update", "existing", "expected"),
    [
        (dict(), None, "description is required"),
        (dict(description="d", status="REJECTED"), None, "requires contradicting evidence"),
        (dict(description="d", status="SUPPORTED"), None, "requires supporting evidence"),
        (
            dict(supporting_evidence_ids=[str(M1)], contradicting_evidence_ids=[str(M1)]),
            _snap("H1", "ACTIVE"),
            "both support and contradict",
        ),
        (dict(status="ACTIVE"), _snap("H1", "REJECTED", contradicting=[L1]), "cannot change"),
        (dict(description="d"), None, "cause_category and component are required"),
        (
            dict(cause_category="dependency"),
            _snap("H1", "ACTIVE", cause_category="deployment"),
            "cause_category is fixed",
        ),
        (
            dict(component="payment-service"),
            _snap("H1", "ACTIVE", component="checkout-service"),
            "component is fixed",
        ),
    ],
)
def test_hypothesis_lifecycle_rules(update, existing, expected):
    problems = check_hypothesis_update(_update(**update), existing, {M1, M2, D1, L1})
    assert any(expected in p for p in problems), problems


def test_creating_a_classified_hypothesis_has_no_problems():
    update = _update(description="d", cause_category="deployment", component="checkout-service")
    assert check_hypothesis_update(update, None, set()) == []


def test_cause_category_is_a_closed_taxonomy():
    with pytest.raises(ValidationError):
        _update(description="d", cause_category="bad deploy", component="checkout-service")


def test_a_valid_transition_has_no_problems():
    assert (
        check_hypothesis_update(
            _update(status="WEAKENED", contradicting_evidence_ids=[str(L1)]),
            _snap("H2", "ACTIVE"),
            {L1},
        )
        == []
    )


# --- stopping criteria ----------------------------------------------------------


def _hypotheses(**overrides):
    base = {
        "H1": _snap("H1", "SUPPORTED", supporting=[M1, D1]),
        "H2": _snap("H2", "WEAKENED", contradicting=[L1]),
    }
    base.update(overrides)
    return base


def test_well_supported_conclusion_with_competitors_weakened_is_accepted():
    assert evaluate_conclusion(_result(), _hypotheses(), TYPES, StoppingCriteria()) == []


@pytest.mark.parametrize(
    ("hypotheses", "result", "fragment"),
    [
        ({"H1": _snap("H1", "SUPPORTED", supporting=[M1, D1])}, _result(), ">= 2 competing"),
        (_hypotheses(H2=_snap("H2", "SUPPORTED", supporting=[L1])), _result(), "H2"),
        (
            _hypotheses(H1=_snap("H1", "SUPPORTED", supporting=[M1])),
            _result(root_ids=[M1]),
            ">= 2 supporting",
        ),
        (
            _hypotheses(H1=_snap("H1", "SUPPORTED", supporting=[M1, M2])),
            _result(root_ids=[M1]),
            "evidence types",
        ),
        (
            _hypotheses(H1=_snap("H1", "ACTIVE", supporting=[M1, D1])),
            _result(),
            "must be SUPPORTED",
        ),
        (_hypotheses(), _result(confidence=0.3), "below"),
        (_hypotheses(), _result(root_ids=[L1]), "root_cause must cite"),
        (_hypotheses(), _result(key="H9"), "does not exist"),
        (
            _hypotheses(H1=_snap("H1", "SUPPORTED", supporting=[M1, D1], contradicting=[L1])),
            _result(),
            "must be explained",
        ),
    ],
)
def test_unmet_stopping_criteria_are_reported(hypotheses, result, fragment):
    unmet = evaluate_conclusion(result, hypotheses, TYPES, StoppingCriteria())
    assert any(fragment in u for u in unmet), unmet


def test_explained_minority_contradiction_is_acceptable():
    hypotheses = _hypotheses(
        H1=_snap("H1", "SUPPORTED", supporting=[M1, D1, M2], contradicting=[L1])
    )
    result = _result(contradictions=[L1])
    assert evaluate_conclusion(result, hypotheses, TYPES, StoppingCriteria()) == []


def test_model_confidence_alone_never_satisfies_the_criteria():
    lone = {"H1": _snap("H1", "SUPPORTED", supporting=[M1])}
    assert evaluate_conclusion(
        _result(root_ids=[M1], confidence=1.0), lone, TYPES, StoppingCriteria()
    )
