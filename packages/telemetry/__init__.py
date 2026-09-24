"""Structured logging and (future) tracing integration points.

See docs/architecture/13-security-boundaries.md and the observability
requirements in the Phase 1 implementation brief: every log line should be
able to carry a correlation/request id, incident id, command idempotency
key, and event id. OpenTelemetry itself is explicitly out of scope for
this phase -- see tracing.py.
"""
