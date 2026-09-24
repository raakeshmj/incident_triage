"""A lightweight metrics seam, the same "prepared integration point, not
wired to a real backend" pattern as `tracing.py` (Phase 1 requirement:
prepare clean integration points; Prometheus itself is out of scope until
its own phase -- see docs/implementation-order.md).

`LoggingMetrics` renders every counter/observation as a structured log
line under a stable `metric.*` event name and stable field names
(`metric`, `value`, plus tags), so a log-based metrics pipeline (or a
human reading `docker compose logs`) can already extract real numbers
today. Swapping in a `prometheus_client`-backed implementation later is a
new class behind the same `Metrics` protocol -- no call site changes.
"""

from __future__ import annotations

from typing import Protocol

from packages.telemetry.logging import get_logger

log = get_logger("metrics")


class Metrics(Protocol):
    def increment(self, name: str, value: int = 1, **tags: object) -> None: ...

    def observe(self, name: str, value: float, **tags: object) -> None: ...

    def gauge(self, name: str, value: float, **tags: object) -> None: ...


class LoggingMetrics:
    def increment(self, name: str, value: int = 1, **tags: object) -> None:
        log.info("metric.increment", metric=name, value=value, **tags)

    def observe(self, name: str, value: float, **tags: object) -> None:
        log.info("metric.observe", metric=name, value=value, **tags)

    def gauge(self, name: str, value: float, **tags: object) -> None:
        log.info("metric.gauge", metric=name, value=value, **tags)


_metrics: Metrics = LoggingMetrics()


def get_metrics() -> Metrics:
    return _metrics
