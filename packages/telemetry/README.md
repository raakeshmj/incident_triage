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
