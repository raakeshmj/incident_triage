# packages/domain

Pure domain models: no FastAPI, no SQLAlchemy, no HTTP or database
imports anywhere in this package -- see `docs/architecture/03-domain-model.md`.

- `enums.py` -- `AlertSource`, `AlertSeverity`, `AlertStatus`, `IncidentStatus`.
- `commands.py` -- `AlertReceivedCommand`, validated (requires
  `labels.service` and `labels.environment`).
- `events.py` -- domain event payload shapes (`AlertReceivedPayload`,
  `IncidentCreatedPayload`, `AlertCorrelatedPayload`).
- `correlation.py` -- deterministic fingerprint function (ADR-0004),
  used for `alerts.fingerprint` and as a new incident's base correlation
  identity.
- `correlation_engine.py` -- the Phase 2 multi-signal correlation engine:
  `CorrelationRule` protocol, the concrete rules (service/environment/
  region/dependency/temporal/alert-type/deployment-proximity-stub), and
  `CorrelationEngine.decide()`, which scores candidate incidents and
  returns an explainable `CorrelationDecision`. See ADR-0015.
- `idempotency.py` -- alert-ingestion's idempotency key derivation
  (06-database-design.md, "Alert deduplication and retries").
- `alert.py`, `incident.py` -- entity read models.
- `views.py` -- API read-model shapes (`IncidentView`, `AlertView`).
- `results.py` -- command result types, also what's stored in
  `processed_commands.result`.

Every function/model here is unit-testable with no infrastructure --
see `tests/unit/domain/`.
