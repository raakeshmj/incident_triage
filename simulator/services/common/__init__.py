"""Shared runtime for the simulated production services.

Deliberately independent of `packages/telemetry`: that package is Incident
Intelligence's own observability library. The services under
`simulator/services/` simulate *third-party* production systems being
observed by Incident Intelligence -- a real checkout/payment/inventory
stack would never import Incident Intelligence's internal packages, so
this module stands on its own (OpenTelemetry SDK + structlog + a minimal
chaos-injection layer), shipped as its own dependency set per Dockerfile.
See docs/adr/0016-simulator-telemetry-independent-of-platform.md.
"""
