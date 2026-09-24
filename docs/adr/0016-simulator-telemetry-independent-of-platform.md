# ADR-0016: Simulated services get their own telemetry stack, not `packages/telemetry`

Status: Accepted

## Context

Phase 3 needs three simulated production services (checkout, payment,
inventory) fully instrumented with OpenTelemetry traces/metrics and
structured logs. `packages/telemetry` already exists and provides
structured logging, request context, and tracing/metrics integration
points -- built for Incident Intelligence's *own* processes
(`apps/api`, `apps/worker`). The question is whether the simulated
services should reuse it or get their own.

## Decision

The simulated services get their own runtime, `simulator/services/common/`
(`telemetry.py`, `chaos.py`, `config.py`, `http_client.py`), with its own
dependency set (`simulator/services/common/requirements.txt`, including
the OpenTelemetry SDK/exporters this phase needs) and its own Dockerfiles.
They do not import `packages/telemetry` or any other `packages/*` module.

## Rationale

`simulator/services/*` don't represent Incident Intelligence components --
they represent the *external, third-party production systems* Incident
Intelligence observes. A real checkout/payment/inventory stack in a real
company would never import Incident Intelligence's internal Python
packages; modeling that boundary in the simulator keeps the trust/data
boundary honest (`docs/architecture/02-component-boundaries.md`'s "why
these boundaries and not others" applies here too, even though the
simulator isn't itself a production component) and avoids two unrelated
things drifting into one shared library:

- `packages/telemetry` can keep evolving to fit Incident Intelligence's
  own needs (e.g. a real OTel tracer eventually replacing `tracing.py`'s
  no-op, per its own docstring) without that change rippling into or being
  constrained by the simulated services' requirements.
- The simulator can freely add heavier dependencies (`opentelemetry-sdk`,
  `psutil`, exporters) that Incident Intelligence's own runtime has no
  reason to carry.
- It's a more realistic architecture to demo against: Incident
  Intelligence's alert-ingestion boundary is exercised the same way it
  would be against a real customer's Prometheus/Alertmanager, not against
  telemetry produced by Incident Intelligence's own logging library.

## Consequences

- Some duplication of small telemetry-wiring concerns (structured JSON
  logging, a middleware pattern) between `packages/telemetry` and
  `simulator/services/common/telemetry.py`. Accepted: they serve different
  processes with different lifecycles, and the duplication is small and
  self-contained.
- `simulator/` is excluded from `mypy` (already true before this phase,
  `pyproject.toml`'s `[tool.mypy] exclude`) and not part of the installed
  `incident-intelligence` package (`[tool.setuptools.packages.find]`) --
  consistent with it never being an internal dependency of anything under
  `apps/`/`packages/`.
