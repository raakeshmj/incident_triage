"""`HeuristicInvestigator`: the deterministic model used by `--mode fake`.

It is a rule-based investigator, not a language model, and it is never
given the scenario: every decision is a pure function of the transcript --
the incident context and the tool results the real evidence path returned.
That makes fake-mode evaluation meaningful as a test of the *harness and
the dataset* (each golden scenario is solvable from its evidence, each hard
negative is not, the grader tells them apart), and says nothing about how
well any real model investigates.

Protocol, driven by what has been observed so far:

1. survey -- health of the service and its dependencies, error rate,
   latency, recent deployments and config changes, error/warning logs;
2. follow up -- resource and traffic metrics; for any unhealthy dependency,
   its own metrics, logs, deployments and config changes;
3. hypothesize -- one hypothesis per candidate cause, supported or weakened
   by specific evidence ids (a change only counts if it precedes the
   anomaly's onset by at most 30 minutes; a cause needs >= 2 evidence types);
4. conclude on a single strongest cause, or declare inconclusive when
   nothing qualifies or two causes are equally supported.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from packages.agents.model import (
    AssistantEntry,
    ContextEntry,
    DecisionRequest,
    ModelTurn,
    ObservationEntry,
)
from packages.domain.investigation import ModelAction

CHANGE_LEAD = timedelta(minutes=30)
CHANGE_LAG = timedelta(minutes=1)
LOG_HINTS = {
    "resource_memory": ("memory", "oom", "gc pause", "heap"),
    "resource_cpu": ("cpu", "worker pool", "queue depth", "saturat", "throttl"),
    "traffic": ("pool exhausted", "rate limit", "retrying", "too many requests", "overload"),
    "infrastructure": ("node", "evicted", "kubelet", "host", "notready", "terminated"),
}


@dataclass
class Observed:
    tool: str
    args: dict[str, Any]
    ok: bool
    evidence_id: str | None
    service: str | None
    data: dict[str, Any]


@dataclass
class Candidate:
    key: str
    category: str
    component: str
    description: str
    supporting: dict[str, str] = field(default_factory=dict)  # evidence id -> type
    contradicting: list[str] = field(default_factory=list)
    qualified: bool = False

    @property
    def types(self) -> set[str]:
        return set(self.supporting.values())

    @property
    def strength(self) -> tuple[int, int]:
        return (len(self.types), len(self.supporting))


def _parse(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


class HeuristicInvestigator:
    provider = "fake"
    model_name = "heuristic-investigator"

    def __init__(self) -> None:
        self.requests: list[DecisionRequest] = []

    # --- reading the transcript -----------------------------------------------------

    def _read(
        self, request: DecisionRequest
    ) -> tuple[dict[str, Any], list[Observed], list[str], set[str]]:
        context: dict[str, Any] = {}
        pending: dict[str, ModelAction] = {}
        observed: list[Observed] = []
        notices: list[str] = []
        called: set[str] = set()
        for entry in request.transcript:
            if isinstance(entry, ContextEntry):
                context = _parse(entry.text.split("\n", 1)[1]) or {}
            elif isinstance(entry, AssistantEntry):
                for action in entry.actions:
                    pending[action.call_id] = action
                    called.add(action.name)
            elif isinstance(entry, ObservationEntry):
                for result in entry.results:
                    requested = pending.get(result.call_id)
                    if requested is None:
                        continue
                    body = _parse(result.content) if not result.is_error else None
                    if isinstance(body, dict) and "evidence_id" in body:
                        observed.append(
                            Observed(
                                requested.name,
                                requested.arguments,
                                True,
                                body["evidence_id"],
                                body.get("service"),
                                body.get("data") or {},
                            )
                        )
                    elif requested.name not in ("update_hypotheses",):
                        observed.append(
                            Observed(requested.name, requested.arguments, False, None, None, {})
                        )
                    notices.append(result.content if result.is_error else "")
                notices += entry.notices
        return context, observed, notices, called

    # --- decisions ----------------------------------------------------------------------

    def decide(self, request: DecisionRequest) -> ModelTurn:
        self.requests.append(request)
        context, observed, notices, called = self._read(request)
        service = context["incident"]["service"]
        dependencies = context.get("service_topology", {}).get("calls", [])
        final_turn = any("FINAL TURN" in n for n in notices)
        turn = len([e for e in request.transcript if isinstance(e, AssistantEntry)])

        if any("Conclusion not accepted" in n for n in notices):
            return self._inconclusive(["the conclusion did not meet the stopping criteria"])
        if "update_hypotheses" in called:
            return self._finish(service, observed)
        if final_turn:
            return self._finish(service, observed, hypothesize_first=True)
        if turn == 0:
            return self._calls(turn, self._survey(service, dependencies), "Surveying the service.")
        if turn == 1:
            follow = self._follow_up(service, observed)
            if follow:
                return self._calls(turn, follow, "Following up on what looks abnormal.")
        return self._hypothesize(service, observed)

    @staticmethod
    def _calls(turn: int, calls: list[tuple[str, dict[str, Any]]], text: str) -> ModelTurn:
        return _turn(
            [
                ModelAction(call_id=f"t{turn}c{i}", name=n, arguments=a)
                for i, (n, a) in enumerate(calls)
            ],
            text,
        )

    @staticmethod
    def _survey(service: str, dependencies: list[str]) -> list[tuple[str, dict[str, Any]]]:
        calls: list[tuple[str, dict[str, Any]]] = [
            ("get_service_health", {"service": service}),
            ("get_metric_window", {"metric": "error_rate", "service": service}),
            ("get_metric_window", {"metric": "latency_p95", "service": service}),
            ("get_recent_deployments", {"service": service}),
            ("get_config_changes", {"service": service}),
            ("get_logs", {"service": service, "severities": ["ERROR", "WARNING", "CRITICAL"]}),
        ]
        calls += [("get_service_health", {"service": d}) for d in dependencies]
        return calls

    @staticmethod
    def _follow_up(service: str, observed: list[Observed]) -> list[tuple[str, dict[str, Any]]]:
        calls: list[tuple[str, dict[str, Any]]] = [
            ("get_metric_window", {"metric": m, "service": service})
            for m in ("request_rate", "cpu_usage", "memory_usage")
        ]
        unhealthy = [
            o.service
            for o in observed
            if o.tool == "get_service_health"
            and o.ok
            and o.service not in (None, service)
            and o.data.get("status") in ("degraded", "down")
        ]
        severities = ["ERROR", "WARNING", "CRITICAL"]
        for dep in unhealthy:
            calls += [
                ("get_metric_window", {"metric": "dependency_error_rate", "service": service}),
                ("get_metric_window", {"metric": "error_rate", "service": dep}),
                ("get_metric_window", {"metric": "latency_p95", "service": dep}),
                ("get_recent_deployments", {"service": dep}),
                ("get_config_changes", {"service": dep}),
                ("get_logs", {"service": dep, "severities": severities}),
            ]
        unique: list[tuple[str, dict[str, Any]]] = []
        for call in calls:
            if call not in unique:
                unique.append(call)
        return unique

    # --- evidence reading helpers ------------------------------------------------------

    @staticmethod
    def _metric(observed: list[Observed], service: str, metric: str) -> Observed | None:
        return next(
            (
                o
                for o in observed
                if o.ok
                and o.tool == "get_metric_window"
                and o.args.get("metric") == metric
                and o.service == service
            ),
            None,
        )

    @staticmethod
    def _health(observed: list[Observed], service: str) -> Observed | None:
        return next(
            (
                o
                for o in observed
                if o.ok and o.tool == "get_service_health" and o.service == service
            ),
            None,
        )

    @staticmethod
    def _of(observed: list[Observed], tool: str, service: str) -> Observed | None:
        return next((o for o in observed if o.ok and o.tool == tool and o.service == service), None)

    @staticmethod
    def _onset(metric: Observed | None) -> datetime | None:
        """When the series first departs from its baseline (>= 50%)."""
        if metric is None:
            return None
        data = metric.data
        baseline = (data.get("baseline") or {}).get("avg")
        if baseline is None:
            return None
        for series in data.get("series", []):
            for point in series.get("points", []):
                if not isinstance(point, list) or point[1] is None:
                    continue
                if abs(point[1] - baseline) > max(abs(baseline) * 0.5, 1e-6):
                    return datetime.fromisoformat(point[0])
        return None

    @staticmethod
    def _rising(metric: Observed | None, ratio: float = 1.5) -> bool:
        if metric is None:
            return False
        data = metric.data
        baseline = (data.get("baseline") or {}).get("avg")
        peak = (data.get("window_stats") or {}).get("max")
        if baseline is None or peak is None:
            return False
        return peak > baseline * ratio + 1e-9

    def _anomaly_onset(self, observed: list[Observed], service: str) -> datetime | None:
        onsets = [
            self._onset(self._metric(observed, service, m))
            for m in ("error_rate", "latency_p95", "memory_usage", "cpu_usage", "request_rate")
        ]
        present = [o for o in onsets if o is not None]
        return min(present) if present else None

    def _log_hits(
        self, observed: list[Observed], service: str, needles: tuple[str, ...]
    ) -> Observed | None:
        logs = self._of(observed, "get_logs", service)
        if logs is None:
            return None
        for group in logs.data.get("representative", []):
            message = str(group.get("message", "")).lower()
            if group.get("severity") in ("ERROR", "WARNING", "CRITICAL") and any(
                n in message for n in needles
            ):
                return logs
        return None

    def _error_logs(self, observed: list[Observed], service: str) -> Observed | None:
        logs = self._of(observed, "get_logs", service)
        if logs is not None and (logs.data.get("error_lines_total") or 0) > 0:
            return logs
        return None

    # --- candidate causes --------------------------------------------------------------

    def _candidates(self, service: str, observed: list[Observed]) -> list[Candidate]:
        out: list[Candidate] = []
        services = [service] + sorted(
            {o.service for o in observed if o.ok and o.service and o.service != service}
        )
        for target in services:
            onset = self._anomaly_onset(observed, target) or (
                self._anomaly_onset(observed, service) if target == service else None
            )
            anomaly = next(
                (
                    m
                    for m in (
                        self._metric(observed, target, "error_rate"),
                        self._metric(observed, target, "latency_p95"),
                    )
                    if self._rising(m)
                ),
                None,
            )
            for tool, category, field_name, at_key in (
                ("get_recent_deployments", "deployment", "deployments_in_window", "deployed_at"),
                ("get_config_changes", "configuration", "changes_in_window", "changed_at"),
            ):
                record = self._of(observed, tool, target)
                if record is None:
                    continue
                c = Candidate(
                    key=f"{category[:6]}_{target}"[:16].replace(".", "_"),
                    category=category,
                    component=target,
                    description=f"A recent {category} change to {target} caused the incident.",
                )
                changes = record.data.get(field_name) or []
                aligned = [
                    ch
                    for ch in changes
                    if onset is not None
                    and onset - CHANGE_LEAD
                    <= datetime.fromisoformat(ch[at_key])
                    <= onset + CHANGE_LAG
                ]
                if aligned and anomaly is not None:
                    c.supporting[record.evidence_id or ""] = category
                    c.supporting[anomaly.evidence_id or ""] = "metric"
                    logs = self._error_logs(observed, target)
                    if logs is not None:
                        c.supporting[logs.evidence_id or ""] = "log"
                else:
                    c.contradicting.append(record.evidence_id or "")
                out.append(c)

        for dep in services[1:]:
            health = self._health(observed, dep)
            if health is None:
                continue
            category = "database" if dep.endswith("-db") else "dependency"
            c = Candidate(
                key=f"dep_{dep}"[:16].replace(".", "_"),
                category=category,
                component=dep,
                description=f"{dep}, a dependency of {service}, is failing or slow.",
            )
            if health.data.get("status") in ("degraded", "down"):
                c.supporting[health.evidence_id or ""] = "metric"
                dependency_errors = self._metric(observed, service, "dependency_error_rate")
                if self._rising(dependency_errors):
                    c.supporting[dependency_errors.evidence_id or ""] = "metric"  # type: ignore[union-attr]
                mention = self._log_hits(observed, service, (dep,))
                if mention is not None:
                    c.supporting[mention.evidence_id or ""] = "log"
            else:
                c.contradicting.append(health.evidence_id or "")
            out.append(c)

        for category, metric, threshold in (
            ("resource_cpu", "cpu_usage", 0.85),
            ("resource_memory", "memory_usage", 200 * 1024 * 1024),
            ("traffic", "request_rate", None),
        ):
            m = self._metric(observed, service, metric)
            if m is None:
                continue
            c = Candidate(
                key=f"{category}"[:16],
                category=category,
                component=service,
                description=f"{service} is failing because of {category.replace('_', ' ')}.",
            )
            peak = (m.data.get("window_stats") or {}).get("max") or 0
            hot = self._rising(m, 2.0 if threshold is None else 1.3) and (
                threshold is None or peak > threshold
            )
            if hot:
                c.supporting[m.evidence_id or ""] = "metric"
                hint = self._log_hits(observed, service, LOG_HINTS[category])
                if hint is not None:
                    c.supporting[hint.evidence_id or ""] = "log"
            else:
                c.contradicting.append(m.evidence_id or "")
            out.append(c)

        health = self._health(observed, service)
        if health is not None and health.data.get("status") == "down":
            c = Candidate(
                key="infra",
                category="infrastructure",
                component=service,
                description=f"The platform running {service} failed.",
            )
            c.supporting[health.evidence_id or ""] = "metric"
            hint = self._log_hits(observed, service, LOG_HINTS["infrastructure"])
            if hint is not None:
                c.supporting[hint.evidence_id or ""] = "log"
            out.append(c)

        keys: set[str] = set()
        for c in out:
            base, n = c.key[:14], 1
            while c.key in keys:
                n += 1
                c.key = f"{base}{n}"
            keys.add(c.key)
            c.supporting.pop("", None)
            c.qualified = len(c.supporting) >= 2 and len(c.types) >= 2
        # a failing dependency whose own change explains it is proximate, not root
        root_components = {
            c.component
            for c in out
            if c.qualified and c.category in ("deployment", "configuration")
        }
        for c in out:
            if c.category in ("dependency", "database") and c.component in root_components:
                c.qualified = False
        return out

    # --- hypotheses and the end -----------------------------------------------------------

    def _hypothesize(self, service: str, observed: list[Observed]) -> ModelTurn:
        candidates = self._candidates(service, observed)
        if not candidates:
            return self._inconclusive(["no usable evidence was returned"])
        best = self._best(candidates)
        updates = []
        for c in candidates:
            if c is best:
                status, confidence = "SUPPORTED", 0.8
            elif c.qualified and best is None:
                status, confidence = "ACTIVE", 0.5  # unresolved: equally supported
            else:
                status, confidence = "WEAKENED", 0.1
            updates.append(
                {
                    "key": c.key,
                    "description": c.description,
                    "cause_category": c.category,
                    "component": c.component,
                    "status": status,
                    "confidence": confidence,
                    "supporting_evidence_ids": sorted(c.supporting),
                    "contradicting_evidence_ids": sorted(c.contradicting),
                    "rationale": f"{len(c.supporting)} supporting item(s) of types "
                    f"{sorted(c.types)}; {len(c.contradicting)} contradicting.",
                }
            )
        return _turn(
            [ModelAction(call_id="hyp", name="update_hypotheses", arguments={"updates": updates})],
            "Recording competing hypotheses.",
        )

    @staticmethod
    def _best(candidates: list[Candidate]) -> Candidate | None:
        qualified = sorted(
            (c for c in candidates if c.qualified), key=lambda c: c.strength, reverse=True
        )
        if not qualified:
            return None
        if len(qualified) > 1 and qualified[0].strength == qualified[1].strength:
            return None  # two equally supported causes: not decidable
        return qualified[0]

    def _finish(
        self, service: str, observed: list[Observed], hypothesize_first: bool = False
    ) -> ModelTurn:
        candidates = self._candidates(service, observed)
        best = self._best(candidates)
        if best is None:
            qualified = [c.key for c in candidates if c.qualified]
            gaps = (
                [f"equally supported causes: {', '.join(qualified)}"]
                if len(qualified) > 1
                else ["no candidate cause is supported by two independent evidence types"]
            )
            return self._inconclusive(gaps)
        if hypothesize_first:
            return self._inconclusive(["out of budget before hypotheses were recorded"])
        support = sorted(best.supporting)
        metric_ids = [e for e, t in best.supporting.items() if t == "metric"] or support
        rca = {
            "incident_summary": {
                "text": f"{service} degraded; evidence points to {best.category} "
                f"in {best.component}.",
                "evidence_ids": support,
            },
            "impact": {
                "text": f"{service} requests were affected.",
                "evidence_ids": metric_ids[:1],
            },
            "affected_services": [{"service": service, "evidence_ids": metric_ids[:1]}],
            "timeline": [
                {
                    "at": _first_time(observed, support),
                    "event": f"{best.category} anomaly observed in {best.component}",
                    "evidence_ids": support[:1],
                }
            ],
            "root_cause": {"text": best.description, "evidence_ids": support},
            "unresolved_questions": [],
            "recommended_next_diagnostic_action": f"Confirm with the owners of {best.component}.",
        }
        return _turn(
            [
                ModelAction(
                    call_id="conclude",
                    name="conclude_investigation",
                    arguments={"selected_hypothesis_key": best.key, "confidence": 0.8, "rca": rca},
                )
            ],
            "Concluding.",
        )

    @staticmethod
    def _inconclusive(gaps: list[str]) -> ModelTurn:
        return _turn(
            [
                ModelAction(
                    call_id="inconclusive",
                    name="declare_inconclusive",
                    arguments={"reason": gaps[0], "evidence_gaps": gaps},
                )
            ],
            "Declaring the investigation inconclusive.",
        )


def _first_time(observed: list[Observed], ids: list[str]) -> str:
    for o in observed:
        if o.evidence_id in ids:
            window = o.data.get("window") or {}
            if window.get("end"):
                return str(window["end"])
            if o.data.get("at"):
                return str(o.data["at"])
    return "1970-01-01T00:00:00+00:00"


def _turn(actions: list[ModelAction], text: str) -> ModelTurn:
    return ModelTurn(
        text=text,
        actions=actions,
        stop_reason="tool_use",
        usage={"input_tokens": 0, "output_tokens": 0},
        provider_payload={
            "content": [{"type": "text", "text": text}]
            + [
                {"type": "tool_use", "id": a.call_id, "name": a.name, "input": a.arguments}
                for a in actions
            ]
        },
        served_model="heuristic-investigator",
        latency_ms=0,
    )
