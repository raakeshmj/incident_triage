# ADR-0004: Alert correlation is deterministic, not LLM-based

Status: Accepted

## Context

Deciding whether a new alert belongs to an existing incident happens on
every alert, at high frequency, and directly determines what row gets
locked and mutated. It's also foundational — if correlation is wrong,
everything downstream (investigation, evidence, RCA) is scoped to the
wrong set of alerts.

## Decision

Correlation is a deterministic function of alert labels, service topology,
and a time window — a fingerprint/rule-based match (`correlation_key`),
implemented as plain code in `incident-core`, not a Claude call.

## Alternatives considered

- **LLM-based correlation** (ask Claude "do these alerts describe the same
  incident?"): rejected — introduces latency, cost, and non-determinism
  into the highest-frequency, most state-sensitive path in the system,
  where a wrong answer corrupts the very serialization key
  (`correlation_key`) the database uses for concurrency control. It also
  violates the core rule that the LLM must never own system state:
  correlation *is* a state-owning decision (it decides which aggregate an
  alert belongs to).

## Consequences

Correlation is fast, cheap, fully unit-testable, and reproducible — the
same set of alerts always groups the same way for a given rule version.
The cost: correlation rules need deliberate engineering effort (label
matching, service-topology awareness, tuning the time window) rather than
"let the model figure it out," and will need iteration as real alert
patterns are observed. This is accepted as the right place to spend that
effort, not a shortcut avoided.
