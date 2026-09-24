# Critical Review

This is a self-critique of the design in `docs/architecture/` and
`docs/adr/`, organized by the specific failure categories requested. For
each: what could go wrong, what the design already does about it, and —
where relevant — what's still an open risk that implementation needs to
watch for rather than something fully solved on paper.

## Revision note (post-architecture-review corrections)

A subsequent architecture review caught real inconsistencies this
document's first pass missed, since fixed:

- **Alert ownership was self-contradictory.** `02-component-boundaries.md`'s
  ownership table listed `alert-ingestion` as inserting into `alerts` while
  its own service description said it only forwards a command — a genuine
  contradiction, not a documentation nit. Fixed: `incident-core` is now
  unambiguously the sole writer of `alerts`, persisting and correlating in
  one transaction. See item 1 below and ADR-0004.
- **`policy-engine`'s purity claim didn't match its own example rules** —
  rules referenced ambient lookups (`incident.environment`,
  `remediation_rate(...)`) that a side-effect-free function can't perform.
  Fixed via an explicit, immutably-captured `PolicyEvaluationContext` — see
  ADR-0012 and `09-remediation-policy-boundaries.md`. This was a real gap
  in the original design, not just an open risk; it's called out here
  rather than folded silently into item 1's mitigation text.
- **`RCA_READY` could previously be reached by an inconclusive
  investigation** — the original state machine's guard used "OR explicit
  'no confident hypothesis' result" for the `RCA_READY` transition, which
  meant "an RCA is ready" and "the investigation gave up" were
  conflated. Fixed: inconclusive/insufficient-evidence outcomes now route
  directly to `ESCALATED`, never through `RCA_READY`. See
  `04-incident-state-machine.md`.
- `verifications.evidence_ids` (a bare `UUID[]`) is now a normalized,
  FK-enforced `verification_evidence` join table; `processed_commands` is
  now scoped by `(command_type, idempotency_key)`; `alerts` now has a
  `(source, external_id)` dedup constraint; and item 1's database-level
  enforcement recommendation is now implemented as a design decision
  (ADR-0013), not just a recommendation — see the updates within each
  section below.

## 1. State ownership

**Risk**: multiple writers to the same aggregate cause lost updates or
contradictory state.

**Mitigation in design**: `incident-core` is the sole writer of every
incident-related table, **including `alerts`** — the ownership table in
`02-component-boundaries.md` previously contradicted this for `alerts`
specifically (it listed `alert-ingestion` as an inserter), which has since
been corrected: `alert-ingestion` holds no database credentials at all and
only ever sends an `AlertReceivedCommand`. Every other service
communicates via commands that `incident-core` validates and applies.
`investigation-agent` cannot write hypotheses or proposals directly — it
submits a command; `incident-core` decides what to persist.

**Resolved**: the database-level enforcement this section originally only
recommended is now a design decision, not just a recommendation — ADR-0013
puts `incident-core` and `evidence-service` in separate logical Postgres
schemas within a shared v1 instance, each behind its own role with grants
scoped to only its own schema. A bug or a "just this once" shortcut in one
service's code now fails with a permission error rather than quietly
succeeding, closing the gap this section originally flagged as unaddressed.
Full physical isolation (separate instances) remains deferred until scale
requires it — see ADR-0013's consequences for what that leaves accepted in
the meantime (shared connection/resource limits, shared failure domain).

## 2. Race conditions

**Identified races and mitigations**:

| Race | Mitigation |
|---|---|
| Two alerts correlate to a new incident simultaneously | Partial unique index on `correlation_key` for open incidents (`06-database-design.md`); the losing insert gets a constraint violation and retries as "link to existing" |
| Investigation completes while a human is mid-approval on a *previous* proposal for the same incident | Each `RemediationProposal` is scoped to one `Investigation`; a new investigation only starts after the current one reaches a terminal outcome per the state machine, so this specific interleaving shouldn't arise — but see open risk below |
| Verification completes while a human triggers manual `ESCALATED` | `ESCALATED` is reachable from every non-terminal state; the state machine's optimistic-concurrency check means whichever transition commits first wins, and the loser's caller sees a version conflict and reloads current state rather than blindly retrying its original intent |
| Two `AlertLinked` commands for the same alert (duplicate webhook delivery) | `alerts.fingerprint` + `processed_commands` idempotency ledger |

**Open risk**: the state machine document doesn't yet define what happens
if a new alert (potentially raising severity, or indicating the *same*
underlying issue is still firing) arrives while the incident is in
`VERIFYING` or `REMEDIATION_IN_PROGRESS`. Today's design treats
`AlertLinked` as orthogonal to the state machine (§04, "Alerts arriving
mid-lifecycle") — that's fine for severity, but if the alert indicates the
fix clearly hasn't worked, waiting for the verification window to elapse
before reacting could be slower than reacting immediately. Recommend an
explicit rule in implementation: a new *firing* alert matching the same
`correlation_key` during `VERIFYING` shortens the verification window's
remaining patience but does not preempt it outright (avoid flapping causing
premature failure calls).

## 3. Event ordering

**Mitigation**: per-`incident_id` ordering is guaranteed via the outbox's
global `sequence` plus per-incident-sharded consumer groups (ADR-0003);
cross-incident ordering is explicitly not promised or needed.

**Open risk**: the "per-incident-sharded consumer group" mechanism is
described at the level of intent, not mechanism, in `05-event-model.md`.
Redis Streams doesn't have native partitioning like Kafka; achieving
per-key ordering with multiple consumer instances typically means either
(a) one stream per incident (unbounded stream count, needs a reaping
strategy for closed incidents) or (b) a fixed number of streams with
consistent hashing on `incident_id` (bounded, but a consumer failure
affects all incidents hashed to that shard until recovery). This is a real
design decision still to be made at implementation time, not just a detail
— flagging it explicitly so it isn't glossed over. Recommend (b) with a
modest, fixed shard count as the starting point.

## 4. Retries and idempotency

**Mitigation**: covered comprehensively in ADR-0011 — idempotency keys on
every command, optimistic concurrency on `incidents`, unique constraint on
`executions.idempotency_key`, executor adapters required to use
target-system idempotency primitives. Since the first pass of this review,
two refinements closed smaller gaps: `processed_commands` is now keyed by
`(command_type, idempotency_key)` rather than the idempotency key alone
(closing a theoretical cross-command-type collision), and `alerts` now has
a `(source, external_id)` partial unique index as a second, database-level
line of defense against duplicate alert rows from source retries,
independent of whether the command-level idempotency key was derived
correctly — see `06-database-design.md`, "Alert deduplication and
retries."

**Open risk**: idempotency of the *investigation* itself is weaker than
idempotency of *state transitions*. If `investigation-agent` crashes after
calling several evidence-service tools but before calling
`submit_findings`, the evidence records it already created remain (they're
immutable and harmless to leave orphaned), but the `Investigation` row
needs a liveness mechanism — a timeout that transitions `running` →
`failed` if no result arrives within the wall-clock budget plus grace
period — otherwise an incident can get stuck in `INVESTIGATING`
indefinitely with no forward progress and no error. This needs to be an
explicit watchdog in `incident-core` (a scheduled job checking for
`investigations.status = 'running'` past `started_at + budget + grace`),
not just "the agent reports failure" — the agent crashing is exactly the
case where it *can't* report anything.

## 5. Unsafe agent permissions

**Mitigation**: this is the most thoroughly addressed area —
`investigation-agent` has no DB access and no write credentials to
anything (ADR-0006); all tools are read-only proxies through
`evidence-service` (ADR-0005); remediation is expressed only as data
referencing a pre-reviewed `action_catalog` entry (ADR-0008); policy and
approval sit between proposal and execution unconditionally (ADR-0007,
ADR-0009).

**Open risk**: `evidence-service`'s tool query surfaces need to be *scoped
to the incident's own service/environment* server-side, not just
constrained in shape. The design says this (`13-security-boundaries.md`,
point 4) but it's worth stating as a hard requirement: if
`get_logs(service, filter, range)` trusts the `service` parameter from the
model without cross-checking it against the incident's actual affected
service(s), a manipulated or simply confused model could pull logs for an
unrelated service. Implementation must validate `service` against the
incident's known scope (with a narrow, explicit escape hatch for
legitimately checking an upstream/downstream dependency — logged and
still scoped to a pre-declared service topology, not arbitrary).

## 6. Hallucinated evidence

**Mitigation**: this is the second most thoroughly addressed area — see
ADR-0005 and `08-evidence-model.md`. Structural enforcement (citations must
resolve to real stored records) rather than a prompting technique.

**Open risk**: structural citation validity doesn't guarantee *semantic*
groundedness — a model could cite a real evidence record that doesn't
actually support the claim it's attached to (e.g. citing a deploy event as
supporting a hypothesis about a database issue, when the deploy is
unrelated). This is why `11-evaluation-architecture.md` includes an
LLM-as-judge groundedness metric with human calibration, rather than
treating citation-existence as sufficient. This gap is inherent to using
an LLM at all and is mitigated, not eliminated — worth stating plainly
rather than implying the evidence model fully solves hallucination. The
practical backstop is that every RCA report is human-reviewable (evidence
links render as inspectable references, not hidden justification), so a
weakly-grounded citation is visible to a reviewer, not just to the eval
harness.

## 7. Observability gaps

**Mitigation**: OpenTelemetry is in the stack; `correlation_id` (=
`incident_id`) and `causation_id` on every event enable distributed tracing
across the whole investigate→remediate→verify chain; token usage,
tool-call count, and wall-clock are tracked per investigation.

**Open risk**: the architecture docs don't yet specify **service-level
metrics and alerting on the platform itself** — e.g., what happens if
`evidence-service` is slow or degraded (not down, just slow), causing
investigations to silently eat their time budget on tool-call latency
rather than reasoning. Recommend, as an explicit requirement for
implementation: each service exports Prometheus metrics for its own
request latency/error rate/queue depth, and the platform's own health is
monitored by the same Prometheus/Grafana stack it uses to monitor the
services it triages — with a clear answer for "who triages the triager"
(likely: a much simpler, human-facing set of standard alerts, deliberately
*not* fed back into this same autonomous pipeline, to avoid a
self-referential failure mode where the system needs to diagnose its own
outage using the parts of itself that are down).

## 8. Evaluation gaps

**Mitigation**: fixture-replay harness with root-cause and groundedness
metrics, policy-safety adversarial suite, CI gating (ADR-0010).

**Open risk**: the golden dataset starts small and human-curated, which
means early eval coverage will be narrow relative to the space of real
incidents. There's also a cold-start problem: before any real incidents
have been handled, there's no organic source of fixtures, only
synthetically constructed ones, which may not capture the messiness of
real telemetry (noisy logs, red herrings, multiple simultaneous
unrelated issues). Recommend treating the first weeks of production
operation (shadow mode — investigating real incidents without ever
proposing remediation, see implementation order) explicitly as a fixture-
generation phase, not just a soak test.

## 9. Circular dependencies

Checked against the component table in `02-component-boundaries.md`:

- `incident-core → investigation-agent → evidence-service`: one direction,
  no cycle. `evidence-service` never calls back into `incident-core`
  except for the one read-only exception noted below.
- `incident-core → policy-engine`: one direction, `policy-engine` calls
  nothing.
- `incident-core → remediation-executor → (incident-core, via command)`:
  this is the one place that looks like a cycle (`incident-core` dispatches
  to the executor, the executor reports back to `incident-core`) — but it's
  not a synchronous cycle, it's a request/eventual-callback pattern
  identical to any async job pattern, and it doesn't create a deadlock
  risk because the executor's callback is a new, independent
  command, not a nested call within the original dispatch.
- **The one genuine cross-call to flag**: `evidence-service`'s
  `search_historical_incidents` tool calls `incident-core`'s read API
  (`08-evidence-model.md`). Combined with `incident-core → investigation-agent
  → evidence-service`, this makes `incident-core` both the caller (at the
  top) and, transitively, a callee (at the bottom) of the same
  investigation flow. It's not a deadlock risk (it's a plain synchronous
  read, not a reentrant write, and it happens on a different investigation
  than any in-flight write), but it does mean `incident-core` must treat
  that particular endpoint as public API with its own rate limiting and
  never assume "only external callers hit this," and it means
  `evidence-service` has a runtime dependency on `incident-core`'s
  availability that isn't obvious from the "evidence-service only talks to
  external systems" framing elsewhere in the docs. Worth naming explicitly
  rather than leaving implicit.

## Summary of concrete follow-ups for implementation (not just this doc)

1. ~~Enforce single-writer ownership with database-level permissions, not
   only code discipline.~~ **Addressed** — ADR-0013 (per-service Postgres
   roles scoped to logical schemas within the shared v1 instance).
2. Decide the concrete Redis Streams sharding mechanism for per-incident
   ordering (fixed shard count + consistent hash on `incident_id`
   recommended) before building the outbox relay.
3. Add an explicit `incident-core` watchdog for stuck `INVESTIGATING`
   states (agent crash / never responds).
4. Enforce server-side service/environment scoping on every
   `evidence-service` query parameter, not just shape-level schema
   validation.
5. Define platform self-monitoring (Prometheus on the services
   themselves) as separate and simpler than the autonomous pipeline it
   monitors — no self-referential triage of the triager.
6. Treat early production operation as shadow-mode fixture generation for
   the eval harness, not only as a soak test.
7. Document `evidence-service`'s dependency on `incident-core`'s read API
   (via `search_historical_incidents`) explicitly as a named exception to
   the "evidence-service only calls external systems" framing.
