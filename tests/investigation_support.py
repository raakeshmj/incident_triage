"""Shared builders for investigation tests. Not a test module.

The scripted model here never knows the answer in advance: its later turns
are *functions of what tools returned*, citing only evidence ids that came
back -- the same position a real model is in.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from packages.agents.config import ModelSpec
from packages.agents.engine import InvestigationEngine, RetryPolicy
from packages.agents.fake import FakeInvestigationModel, evidence_ids, turn
from packages.agents.model import DecisionRequest, ModelTurn, ObservationEntry
from packages.domain.investigation import InvestigationBudget, StoppingCriteria
from packages.evidence.adapters.changes import ChangeRegistryAdapter
from packages.evidence.adapters.git import GitAdapter
from packages.evidence.adapters.loki import LokiAdapter
from packages.evidence.adapters.prometheus import PrometheusAdapter
from packages.evidence.adapters.tempo import TempoAdapter
from packages.evidence.scope import ServiceCatalog
from packages.evidence.service import EvidenceService
from tests.factories import make_alert_command

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ServiceCatalog.load(ROOT / "infrastructure/evidence/service-catalog.json")
FAKE_SPEC = ModelSpec(
    provider="fake",
    model="fake-model",
    thinking="none",
    effort=None,
    max_tokens=4096,
    timeout_seconds=10,
    max_retries=0,
)


def prometheus_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("query_range"):
        start = float(request.url.params["start"])
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "result": [{"metric": {}, "values": [[start, "0.3"], [start + 60, "0.3"]]}]
                },
            },
        )
    query = request.url.params["query"]
    value = "1" if "up{" in query else "0.001"
    return httpx.Response(
        200, json={"status": "success", "data": {"result": [{"metric": {}, "value": [1, value]}]}}
    )


def loki_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("query_range"):
        line = json.dumps(
            {"severity": "ERROR", "message": "checkout.failed", "trace_id": "ab" * 16}
        )
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "result": [
                        {
                            "stream": {"service": "checkout-service"},
                            "values": [["1790000000000000000", line]],
                        }
                    ]
                },
            },
        )
    return httpx.Response(200, json={"data": {"result": [{"value": [1, "12"]}]}})


def build_evidence_service(
    session_factory, core, redis_client, *, loki=loki_handler
) -> EvidenceService:
    def client(handler) -> httpx.Client:
        return httpx.Client(base_url="http://canned", transport=httpx.MockTransport(handler))

    return EvidenceService(
        session_factory=session_factory,
        gateway=core,
        catalog=CATALOG,
        prometheus=PrometheusAdapter(client(prometheus_handler)),
        loki=LokiAdapter(client(loki)),
        tempo=TempoAdapter(client(lambda r: httpx.Response(200, json={"traces": []}))),
        changes=ChangeRegistryAdapter(redis_client),
        git=GitAdapter(ROOT),
    )


def open_incident(core, service: str = "checkout-service") -> uuid.UUID:
    result = core.handle_alert_received(
        make_alert_command(
            service=service,
            external_id=str(uuid.uuid4()),
            extra_labels={
                "region": "us-east-1",
                "alertname": "HighErrorRate",
                "alert_type": "availability",
            },
        )
    )
    return result.incident_id


def start(investigations, incident_id, budget: InvestigationBudget | None = None) -> uuid.UUID:
    return investigations.request_investigation(
        incident_id,
        model_provider=FAKE_SPEC.provider,
        model_name=FAKE_SPEC.model,
        model_settings=FAKE_SPEC.settings(),
        budget=budget or InvestigationBudget(),
    ).investigation_id


def make_engine(
    investigations,
    core,
    evidence,
    model: FakeInvestigationModel,
    *,
    owner: str = "worker-a",
    attempts: int = 3,
) -> InvestigationEngine:
    return InvestigationEngine(
        gateway=investigations,
        incidents=core,
        evidence=evidence,
        catalog=CATALOG,
        model_factory=lambda spec: model,
        owner=owner,
        criteria=StoppingCriteria(),
        lease_seconds=120,
        retry=RetryPolicy(attempts=attempts, backoff_seconds=0),
        tool_retry_backoff_seconds=0,
        sleep=lambda _s: None,
    )


# --- the scripted "competing hypotheses" investigation ------------------------------

GATHER = turn(
    ("get_metric_window", {"metric": "error_rate"}),
    ("get_recent_deployments", {}),
    ("get_service_health", {"service": "payment-service"}),
    text="Error rate first, what changed, and whether the payment dependency is healthy.",
)


def _by_tool(request: DecisionRequest) -> dict[str, str]:
    """evidence_id per tool name, from the most recent tool results."""
    last_turn = None
    for entry in request.transcript:
        if hasattr(entry, "actions"):
            last_turn = entry
    found: dict[str, str] = {}
    results: list[Any] = []
    for entry in reversed(request.transcript):
        if isinstance(entry, ObservationEntry) and entry.results:
            results = entry.results
            break
    names = {a.call_id: a.name for a in (last_turn.actions if last_turn else [])}
    for r in results:
        try:
            data = json.loads(r.content)
        except ValueError:
            continue
        if isinstance(data, dict) and "evidence_id" in data:
            found[names.get(r.call_id, "?")] = data["evidence_id"]
    return found


def hypotheses_turn(request: DecisionRequest) -> ModelTurn:
    ids = _by_tool(request)
    return turn(
        (
            "update_hypotheses",
            {
                "updates": [
                    {
                        "key": "H1",
                        "description": "The most recent deployment introduced a regression.",
                        "status": "SUPPORTED",
                        "confidence": 0.75,
                        "supporting_evidence_ids": [
                            ids["get_metric_window"],
                            ids["get_recent_deployments"],
                        ],
                        "rationale": "Error rate rose and a new version was deployed.",
                    },
                    {
                        "key": "H2",
                        "description": "A failing payment dependency caused checkout failures.",
                        "status": "WEAKENED",
                        "confidence": 0.1,
                        "contradicting_evidence_ids": [ids["get_service_health"]],
                        "rationale": "payment-service reports healthy.",
                    },
                ]
            },
        )
    )


def conclude_turn(request: DecisionRequest) -> ModelTurn:
    metric = evidence_ids(request, "metric")[0]
    deploy = evidence_ids(request, "deployment")[0]
    statement = {
        "text": "Checkout errors rose after a deployment.",
        "evidence_ids": [metric, deploy],
    }
    return turn(
        (
            "conclude_investigation",
            {
                "selected_hypothesis_key": "H1",
                "confidence": 0.8,
                "rca": {
                    "incident_summary": statement,
                    "impact": {
                        "text": "Roughly 30% of checkouts failed.",
                        "evidence_ids": [metric],
                    },
                    "affected_services": [
                        {"service": "checkout-service", "evidence_ids": [metric]}
                    ],
                    "timeline": [
                        {
                            "at": datetime.now(UTC).isoformat(),
                            "event": "New version deployed",
                            "evidence_ids": [deploy],
                        }
                    ],
                    "root_cause": {
                        "text": "The deployed version introduced a regression in checkout.",
                        "evidence_ids": [deploy, metric],
                    },
                    "unresolved_questions": ["Which code change inside the release?"],
                    "recommended_next_diagnostic_action": "Diff the release against the last one.",
                },
            },
        )
    )


def happy_script() -> list:
    return [GATHER, hypotheses_turn, conclude_turn]
