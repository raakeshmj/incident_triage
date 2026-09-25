# ADR-0023: Evaluation and replay through the real evidence path

Status: Accepted

## Context

The investigation engine needs regression tests for behaviour (correct
cause, grounded evidence, honest escalation) that are deterministic, cheap,
and don't need live telemetry or a model. 11-evaluation-architecture.md
sketched a `replay` backend inside evidence-service serving frozen fixture
bundles, plus LLM-as-judge scoring.

## Decision

1. **Scenario worlds behind the real adapters.** A golden scenario is a
   small declarative world (metric step changes, logs, change records,
   outages). `ScenarioWorld` serves it as HTTP responses to the *real*
   Prometheus/Loki/Tempo adapters (in-process transport) and as data to
   subclasses of the real change-registry and Git adapters. Scope checks,
   bounds, normalization, hashing, evidence records and refs are all
   production code.
2. **Recordings are exports of the persisted trace**, versioned
   (`recording-v1`), self-contained and secret-scrubbed.
3. **Replay substitutes at the tool surface**, not inside evidence-service:
   recorded model turns + recorded tool results, with everything the
   application decides recomputed and compared (`signature`).
4. **Structured grading, no LLM judge (yet).** Hypotheses carry a cause
   category and component; the grader compares data, not prose.
   Escalation is graded as an outcome with its reason.
5. **A dedicated, disposable evaluation database**, reset before each run,
   so runs can't see each other's history (similar-incident search would
   otherwise leak earlier answers).
6. **Fake mode = a heuristic investigator** that sees only tool results;
   live mode = the configured model, explicitly requested. No
   model-comparison tooling.

## Alternatives considered

- **Replay backend in evidence-service with recorded raw responses.**
  More faithful to backend quirks, but requires recording raw responses
  for every source and still needs a model substitute; the tool-surface
  cut gives deterministic re-execution of everything the application
  decides with far less machinery.
- **Freezing live-captured evidence as fixtures.** Useful later for
  production-derived cases; declarative worlds are reviewable and
  independent of wall-clock time.

## Consequences

- Scenario worlds are only as realistic as their data; the heuristic
  investigator passing them proves solvability, not model quality.
- Replay verifies the deterministic half of the system against current
  code; it cannot say whether a *different* model would decide the same.
