# packages/telemetry

Structured logging and a clean OpenTelemetry integration point.

- `logging.py` -- `configure_logging()` (call once per process) and
  `get_logger(__name__)`. JSON output via structlog.
- `context.py` -- `bind_context(request_id=..., idempotency_key=...,
  incident_id=..., alert_id=..., event_id=...)`, a context manager that
  makes every log line emitted inside it carry those fields automatically.
- `tracing.py` -- `start_span(name, **attrs)`, currently a no-op. Phase 1
  requirement: prepare the integration point, don't wire OpenTelemetry
  itself yet. Swapping the body for a real tracer later doesn't require
  changing any call site.
- `metrics.py` -- `get_metrics()` returns a `Metrics` (`increment`/
  `observe`/`gauge`), currently `LoggingMetrics` (renders each call as a
  structured `metric.*` log line under stable field names). Same
  "prepared integration point" pattern as `tracing.py`: a
  `prometheus_client`-backed implementation is a new class behind the same
  protocol, not a call-site change. Used throughout Phase 2's outbox relay
  and event consumer for publish latency/retries, processing duration/
  failures, dead-letter counts, and correlation decisions/scores.
