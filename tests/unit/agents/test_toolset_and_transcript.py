"""Tool surface and transcript reconstruction (pure parts)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from packages.agents.engine import build_transcript, observation_for
from packages.agents.model import AssistantEntry, ContextEntry, ObservationEntry
from packages.agents.toolset import (
    EVIDENCE_TOOL_ALIASES,
    EVIDENCE_TOOL_NAMES,
    InvestigationToolset,
    compact,
    inline_refs,
)
from packages.domain.investigation import StepKind, StepView
from packages.tools.registry import TOOLS

NOW = datetime(2026, 9, 26, tzinfo=UTC)


def _step(seq, kind, payload, call_id=None, iteration=1):
    return StepView(
        sequence=seq,
        iteration=iteration,
        kind=kind,
        call_id=call_id,
        payload=payload,
        latency_ms=None,
        created_at=NOW,
    )


def test_every_exposed_evidence_tool_is_an_existing_read_only_contract():
    assert set(EVIDENCE_TOOL_ALIASES.values()) <= set(TOOLS)
    names = {t.name for t in InvestigationToolset.definitions()}
    assert names == {
        *EVIDENCE_TOOL_NAMES,
        "update_hypotheses",
        "conclude_investigation",
        "declare_inconclusive",
    }
    for required in (
        "get_service_health",
        "get_metric_window",
        "get_logs",
        "get_traces",
        "get_recent_deployments",
        "get_code_changes",
        "search_similar_incidents",
        "get_incident_evidence",
    ):
        assert required in names


def test_no_tool_schema_accepts_an_incident_id_or_raw_query():
    for tool in InvestigationToolset.definitions():
        text = json.dumps(tool.input_schema)
        assert "$ref" not in text and "$defs" not in text  # self-contained
        properties = tool.input_schema.get("properties", {})
        for forbidden in ("incident_id", "investigation_id", "promql", "logql", "query", "sql"):
            assert forbidden not in properties, (tool.name, forbidden)


def test_inline_refs_resolves_nested_definitions():
    schema = {
        "$defs": {"B": {"type": "object", "properties": {"x": {"type": "string"}}}},
        "type": "object",
        "properties": {"b": {"$ref": "#/$defs/B"}, "bs": {"items": {"$ref": "#/$defs/B"}}},
    }
    inlined = inline_refs(schema)
    assert inlined["properties"]["b"]["properties"]["x"] == {"type": "string"}
    assert "$defs" not in inlined


def test_compact_bounds_what_the_model_sees():
    big = {"evidence_id": "e1", "data": {"series": [{"points": list(range(5000))}]}}
    text = compact(big, limit=500)
    assert len(text) <= 500
    assert "e1" in text


def test_compacted_series_keep_their_shape_first_and_last_points():
    points = [[f"t{i:02d}", 0.01 if i < 50 else 0.3] for i in range(60)]
    big = {"evidence_id": "e1", "data": {"series": [{"points": points}], "pad": "x" * 2500}}
    text = compact(big, limit=3000)
    kept = json.loads(text)["data"]["series"][0]["points"]
    assert kept[0] == ["t00", 0.01] and kept[-2] == ["t59", 0.3]  # last item is the note
    assert "downsampled from 60 points" in kept[-1]
    assert any(p[1] == 0.3 for p in kept[:-1])  # the change is still visible


def test_transcript_is_a_pure_function_of_the_trace():
    steps = [
        _step(1, StepKind.CONTEXT, {"context": {"incident": {"service": "s"}}}, iteration=0),
        _step(2, StepKind.FEEDBACK, {"text": "Budget: turn 1 of 5."}),
        _step(
            3,
            StepKind.MODEL_TURN,
            {
                "text": "t",
                "actions": [
                    {"call_id": "a", "name": "get_logs", "arguments": {}},
                    {"call_id": "b", "name": "update_hypotheses", "arguments": {}},
                ],
                "provider_payload": {"content": []},
            },
        ),
        _step(
            4,
            StepKind.TOOL_CALL,
            {"observation": '{"evidence_id":"e1"}', "is_error": False},
            call_id="a",
        ),
        _step(
            5,
            StepKind.HYPOTHESIS_UPDATE,
            {"applied": [], "rejected": [{"key": "H1", "problems": ["bad id"]}], "hypotheses": []},
            call_id="b",
        ),
        _step(6, StepKind.MODEL_ERROR, {"code": "RateLimitError"}),
        _step(7, StepKind.FEEDBACK, {"text": "Budget: turn 2 of 5."}, iteration=2),
    ]
    transcript = build_transcript(steps)
    assert [type(e) for e in transcript] == [
        ContextEntry,
        ObservationEntry,
        AssistantEntry,
        ObservationEntry,
    ]
    observation = transcript[3]
    assert isinstance(observation, ObservationEntry)
    assert [r.call_id for r in observation.results] == ["a", "b"]
    assert observation.results[1].is_error  # the rejection is shown, never hidden
    assert observation.notices == ["Budget: turn 2 of 5."]
    assert build_transcript(steps) == transcript


def test_rejected_conclusion_observation_lists_unmet_criteria():
    text, is_error = observation_for(
        _step(1, StepKind.CONCLUSION_REJECTED, {"unmet_criteria": ["needs 2 types"]}, call_id="c")
    )
    assert is_error and "needs 2 types" in text
