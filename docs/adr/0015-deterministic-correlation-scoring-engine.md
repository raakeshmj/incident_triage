# ADR-0015: Multi-signal deterministic correlation, advisory-lock concurrency control, and time-bucketed new-incident keys

Status: Accepted

## Context

Phase 1's correlation rule was exact fingerprint equality
(`packages/domain/correlation.py`) — deliberately minimal, with ADR-0004
explicitly anticipating richer rules later. Phase 2 requires that richer
engine: an alert with a *different* signature (different alertname, say)
should still join an existing incident when service, environment, timing,
and other signals say it plausibly belongs there, and the decision must
stay explainable and LLM-free per ADR-0004.

Introducing multi-candidate scoring surfaces two problems Phase 1's exact-
key design didn't have to face:

1. **A race that a unique index alone can't fix.** Phase 1's race
   protection (`incidents_open_correlation_key`, a partial unique index)
   works because "does this belong to an existing incident" was answered
   by one deterministic key lookup. Once the answer requires *reading
   several candidate rows and scoring them*, two concurrent alerts for the
   same service+environment that both see zero pre-existing candidates
   (because neither has committed yet) can each independently decide
   "create a new incident" — a genuine write-skew scenario a unique index
   cannot prevent on its own, since both would-be incidents can validly
   have different data.
2. **A stale match trap.** If a new incident's `correlation_key` is just
   the alert's fingerprint (Phase 1's scheme), and a *late-arriving* alert
   shares that same fingerprint with a long-closed-in-spirit-but-technically-
   still-open incident from hours ago, the partial unique index will
   reject the new INSERT as a conflict — and the fallback path (built for
   genuine races) will silently attach the late alert to the stale
   incident instead of creating a new one, exactly contradicting the
   correlation engine's own (correct) decision that they shouldn't be
   linked. This was caught by
   `tests/integration/test_late_arriving_alert.py` during implementation,
   not anticipated in advance — worth recording plainly.

## Decision

**Correlation is a pure, rule-based scoring function**
(`packages/domain/correlation_engine.py`): `CorrelationEngine.decide(alert,
candidates)` sums named, independently-testable `CorrelationRule`
contributions (same service, same environment, same region, same
dependency, temporal proximity, related/identical alert type, and a
deployment-proximity stub with no data source yet) and compares the best
candidate's score against a threshold (default 0.6). The result is always
`{decision, score, matched_signals, matched_incident_id}` — every decision
traces to named signals, satisfying the same explainability requirement
ADR-0004 established for the original deterministic rule.

**Candidates are bounded by a lookback window**
(`repository.find_open_incident_candidates`, default 15 minutes): only
open incidents whose most recently received alert falls within the window
are even considered. This is what makes "late-arriving alert creates a new
incident" a query-level guarantee, not just a scoring outcome — an
incident with no recent activity is never offered to the engine as a
candidate at all.

**A Postgres advisory transaction lock serializes the decision per
(service, environment)** (`repository.acquire_correlation_lock`,
`pg_advisory_xact_lock`), held for the full "read candidates → decide →
write" sequence. This is what actually fixes problem 1: two concurrent
alerts for the same service+environment are never mid-decision
simultaneously, so the second one always sees the first's already-committed
incident (if any) before deciding. The partial unique index on
`correlation_key` remains as a defense-in-depth backstop (consistent with
this system's general "unique index behind every documented race,"
`06-database-design.md`) in case the lock is ever bypassed by a bug — not
as the primary mechanism anymore.

**A new incident's `correlation_key` is `{fingerprint}:{time_bucket}`**,
where `time_bucket = floor(now / candidate_lookback_seconds)`, using the
same clock and the same window the candidate query itself uses
(`IncidentCoreService`). This fixes problem 2: a late-arriving alert
computing a new incident's key lands in a *different* bucket than a stale
incident created outside the window, so the INSERT never spuriously
conflicts with it — while two genuinely concurrent alerts (same instant,
same bucket) still collide as intended, correctly serialized by the
existing `ON CONFLICT` fallback. `correlation_key` is consequently no
longer a clean, human-legible "the" identity of an alert signature the way
it was in Phase 1 — it's an internal uniqueness token. `IncidentCreatedPayload.correlation_key`
and `IncidentView.correlation_key` reflect whatever value actually got
persisted (`incident_row.correlation_key`), not a recomputed one, so the
API never shows a value that doesn't match storage.

**Correlation timing is injected, not read from `alerts.received_at`.**
`IncidentCoreService` takes an optional `clock: Callable[[], datetime]`
(default `datetime.now(UTC)`), used for both the candidate query's `now`
and the new-incident time bucket. This decouples "when the correlation
decision considers itself to be happening" from "whatever Postgres
assigned as the alert row's insert timestamp" — the same value in
production, but independently controllable in tests, which is how
`tests/integration/test_late_arriving_alert.py` simulates the passage of
time without an actual multi-minute sleep.

## Alternatives considered

- **`SELECT ... FOR UPDATE` on candidate incident rows instead of an
  advisory lock**: rejected — there may be zero candidate rows to lock
  (the exact scenario that causes the race), so row locking cannot cover
  the "no candidates yet, both decide to create" case. An advisory lock,
  keyed by a value independent of whether any row exists yet
  (`service:environment`), covers it.
- **A UUID suffix (the new alert's own id) instead of a time bucket** for
  new-incident key disambiguation: rejected — it would make the unique
  index permanently unable to catch a genuine concurrent-creation race
  either (since two different alerts always have two different ids
  regardless of timing), pushing all race protection onto the advisory
  lock alone. The time bucket preserves the unique index as a meaningful
  backstop for the concurrent case while still disambiguating the stale
  case.
- **Recomputing `correlation_key` for display from the fingerprint alone**:
  rejected — would show a value that could differ from what's actually
  enforced by the unique index, misleading anyone debugging a correlation
  decision from the API or an event payload.

## Consequences

Case F (late-arriving alerts) and Case E (concurrent related alerts) both
have dedicated integration tests
(`tests/integration/test_late_arriving_alert.py`,
`tests/integration/test_correlation_concurrency.py`) and both pass.
The cost: `correlation_key` is no longer a stable, externally-meaningful
identifier across time the way it briefly was in Phase 1 — any future code
or dashboard that wants "the alert signature this incident started from"
should read the initiating alert's `fingerprint` directly, not parse
`correlation_key`. The advisory lock scope (`service:environment`) also
means correlation decisions for the same service+environment are fully
serialized regardless of how unrelated the alerts turn out to be (e.g. two
alerts that will score 0 against each other still wait on each other's
lock) — acceptable at current volume, and a candidate for a finer-grained
lock key (e.g. incorporating region) if it ever becomes a bottleneck.
