"""The 7 chaos scenario definitions.

Each entry documents, in one place, exactly what the requirement asks for:
a trigger (the CLI command), the expected effect (what telemetry moves and
which `infrastructure/prometheus/alerts/` rule it's meant to fire), and how
it starts/stops. The runtime effect itself lives in
`simulator.services.common.chaos.ChaosController` -- this module is the
catalog + defaults + CLI help text, not the effect implementation.

`error-storm` and `bad-configuration` deliberately produce the *same*
observable symptom (an elevated 5xx rate) via different default framing --
one is "the service itself is degraded", the other is "a bad config value
broke a code path" -- because distinguishing the two from telemetry alone
is exactly the job of a future investigation agent (Phase 5), not
something this platform is allowed to hard-code into the alert.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ChaosScenario:
    name: str
    services: tuple[str, ...]
    default_duration_seconds: int
    default_params: dict[str, Any]
    trigger: str
    effect: str
    fires_alert: str

    def params(self, overrides: dict[str, Any] | None) -> dict[str, Any]:
        merged = dict(self.default_params)
        if overrides:
            merged.update(overrides)
        return merged


_SERVICES = ("checkout-service", "payment-service", "inventory-service")

SCENARIOS: dict[str, ChaosScenario] = {
    scenario.name: scenario
    for scenario in [
        ChaosScenario(
            name="bad-deployment",
            services=_SERVICES,
            default_duration_seconds=600,
            default_params={"error_rate": 0.3, "version": "1.1.0-bad", "previous_version": "1.0.0"},
            trigger="`chaos start bad-deployment --service <svc>`",
            effect=(
                "Elevated 5xx rate on <svc>, plus its `service_deployment_info` metric "
                "flips to a new version label at the same moment -- a real rollout of a "
                "broken build."
            ),
            fires_alert="HighErrorRate",
        ),
        ChaosScenario(
            name="high-cpu",
            services=_SERVICES,
            default_duration_seconds=300,
            default_params={"thread_count": 2},
            trigger="`chaos start high-cpu --service <svc>`",
            effect=(
                "<svc> spins busy-loop worker threads, genuinely saturating its CPU; "
                "`process_cpu_usage_ratio` rises for real (not a faked metric value)."
            ),
            fires_alert="CPUSaturation",
        ),
        ChaosScenario(
            name="memory-leak",
            services=_SERVICES,
            default_duration_seconds=300,
            default_params={"mb_per_second": 5},
            trigger="`chaos start memory-leak --service <svc>`",
            effect=(
                "<svc> grows an in-process byte buffer every second; "
                "`process_memory_usage_bytes` climbs monotonically until stopped."
            ),
            fires_alert="MemoryPressure",
        ),
        ChaosScenario(
            name="dependency-failure",
            services=_SERVICES,
            default_duration_seconds=300,
            default_params={"error_rate": 1.0, "timeout_seconds": 3.0},
            trigger="`chaos start dependency-failure --service inventory-service`",
            effect=(
                "The target service (e.g. inventory-service) fails/times out on nearly "
                "every request, so its own error rate rises AND its callers "
                "(payment-service) see `dependency_call_errors_total` climb -- a real "
                "two-service cascade, not a mocked failure."
            ),
            fires_alert="DependencyFailureRate",
        ),
        ChaosScenario(
            name="high-latency",
            services=_SERVICES,
            default_duration_seconds=300,
            default_params={"delay_seconds": 2.0},
            trigger="`chaos start high-latency --service <svc>`",
            effect="<svc> sleeps before responding; p95 latency genuinely rises.",
            fires_alert="HighP95Latency",
        ),
        ChaosScenario(
            name="error-storm",
            services=_SERVICES,
            default_duration_seconds=300,
            default_params={"error_rate": 0.5},
            trigger="`chaos start error-storm --service <svc>`",
            effect="<svc> returns HTTP 500 for a random share of requests.",
            fires_alert="HighErrorRate",
        ),
        ChaosScenario(
            name="bad-configuration",
            services=_SERVICES,
            default_duration_seconds=300,
            default_params={"error_rate": 0.35},
            trigger="`chaos start bad-configuration --service <svc>`",
            effect=(
                "<svc> returns HTTP 500 for a random share of requests -- symptomatically "
                "identical to error-storm; see module docstring for why that's deliberate."
            ),
            fires_alert="HighErrorRate",
        ),
    ]
}


def get(name: str) -> ChaosScenario:
    try:
        return SCENARIOS[name]
    except KeyError:
        raise ValueError(f"unknown chaos scenario {name!r}; choices: {sorted(SCENARIOS)}") from None
