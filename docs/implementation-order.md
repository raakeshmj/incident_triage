# Recommended Implementation Order

Each phase should be shippable and independently testable before starting
the next. Phases 1–4 contain zero LLM involvement — the goal is a fully
correct, deterministic, race-free core before any autonomy is layered on.

**Implementation status note**: the actual build sessions' "Phase 1" and
"Phase 2" map onto this document as follows — Phase 1 (build) delivered
this document's Phase 1 in full. Phase 2 (build) delivered this document's
Phase 1 event-transport items in their production-shaped form (a real
outbox relay with retries/backoff, sharded Redis Streams, consumer groups,
a dead-letter stream, consumer-side idempotency — see ADR-0014) *and*
pulled the multi-signal correlation engine forward from what Phase 1
originally scoped as a minimal fingerprint rule (see ADR-0015) — but did
**not** touch this document's Phase 2 (evidence-service). So "build Phase
2" is a deepening of this document's Phase 1, not this document's Phase 2;
evidence-service, policy-engine, and everything from here on remain
unstarted. Documented here rather than renumbering the phases below, which
were written before either build session and still describe the right next
steps once evidence-service's turn comes.

Build session "Phase 3" is orthogonal to this document's numbering
entirely: it built the *environment* Incident Intelligence observes
(three simulated services, a full local observability stack, real
Prometheus alerting, Alertmanager delivering into the existing
alert-ingestion path — `docs/architecture/14-observability-and-chaos.md`)
rather than a new Incident Intelligence component. It deepens this
document's Phase 0/§12 local-dev environment and gives Phase 1's
alert-ingestion a second, real alert source alongside the synthetic one —
it does not touch this document's actual "Phase 3 — Policy engine and
action catalog," which remains unstarted.

Build session "Phase 4" delivers most of this document's **Phase 2
(evidence-service)**: Prometheus, Loki *and* Tempo adapters plus
deployment, config, Git and incident-history sources; immutable,
content-hashed evidence storage in its own schema with incident-core
references (ADR-0018); and the investigation tool contracts from §07,
built and tested but not connected to any model. Not delivered from that
phase: `record`/`replay` *modes* (live only), and the "hand-authored test
remediation through VERIFYING -> RESOLVED" exit criterion, which needs
remediation (still out of scope). It also implements the state machine's
alert-driven `TRIAGING -> CANCELLED` edge from this document's Phase 1
(ADR-0019).

Build session "Phase 5" delivers this document's **Phase 5 investigation
agent** minus the golden dataset / eval harness (deliberately deferred; the
persisted trace is the hook it will replay): a bounded, resumable,
hypothesis-driven loop with Claude behind a model abstraction
(`INVESTIGATION_MODEL`, default `claude-haiku-4-5` since Phase 6), incident-core
validation of every hypothesis change and conclusion, deterministic stopping
criteria, `TRIAGING → INVESTIGATING → RCA_READY | ESCALATED` driven by a
debounce scheduler and events. Departures from §07 are in ADR-0020/0021; no
remediation or action-catalog validation (that needs this document's Phase 3).
See `architecture/15-investigation-engine.md`.

Build session "Phase 6" delivers the evaluation and replay framework
(`architecture/11-evaluation-architecture.md`, "Phase 6: as built";
ADR-0023): 17 golden scenarios (10 root-cause, 7 must-escalate) served
through the real evidence path, recorded investigations, deterministic
replay, structured grading, repeated runs and aggregation, and a CLI. It
also caches the stable prompt prefix (ADR-0022) and makes the runtime
provider/model a placeholder configuration. Deferred: any live-provider
run (no credential yet), LLM-as-judge and CI gating on eval thresholds.

Build session "Phase 7" delivers this document's Phase 3 (policy engine
and action catalog) and the approval + execution parts of Phase 4, against
the simulator only: deterministic RCA-driven planning, a 5-action catalog,
a pure policy engine with stored contexts, human approval bound to the
exact proposal, a leased / reconciled / bounded executor, kill switches and
an immutable remediation timeline (`architecture/09-...md`, "Phase 7: as
built"; ADR-0024). No automatic execution; verification is requested but
not performed; no approval UI (API only).

Build session "Phase 8" (the final planned build session) closes the loop:
deterministic, evidence-based verification with pre-remediation baselines,
per-action windows and consecutive-success rules; VERIFYING → RESOLVED only
on a PASSED verification, VERIFICATION_FAILED → re-investigation (bounded)
or ESCALATED, TIMED_OUT → ESCALATED (`architecture/10-verification-design.md`,
ADR-0025). It also adds the read API and the operations console
(`apps/dashboard`, Carbon; `frontend/design-workflow.md`), full-lifecycle
evaluation and replay, worker heartbeats and `docs/operations.md`. Still not
built: pre-approved `ALLOW` execution, shadow mode against real production
systems, identity-provider auth, `CLOSED`/`SUPPRESSED`.

## Phase 0 — Foundations (no services yet)

- `libs/schemas`: Pydantic models for the domain entities, event envelope,
  and command payloads — shared by every service, defined once.
- Postgres schema migrations for `incident-core` (§06).
- `docker-compose` local dev environment (§12) with Postgres, Redis, and
  empty service stubs that just health-check.
- CI skeleton: lint, type-check, migration check on every PR.

*Exit criteria*: `make dev-up` brings up a working Postgres + Redis with
migrations applied; nothing else needs to work yet.

## Phase 1 — Deterministic core: ingestion, correlation, state machine

- `alert-ingestion`: webhook auth, schema validation, normalize to `Alert`
  shape, send `AlertReceivedCommand` to `incident-core` — this service
  never touches a database.
- `incident-core`: sole persistence of `Alert` (including the
  `(source, external_id)` dedup index), correlation module (deterministic
  fingerprinting), Incident aggregate, the full state machine transition
  table minus any step that depends on investigation/remediation (i.e.
  `TRIAGING` → `INVESTIGATING`/`SUPPRESSED`/`CANCELLED` only for now),
  outbox writes, the `(command_type, idempotency_key)`-scoped idempotency
  ledger, optimistic concurrency.
- Outbox relay → Redis Streams.
- `notification-service`: consume `IncidentCreated`/`IncidentStatusChanged`,
  post to Slack/webhook.

*Exit criteria*: synthetic alerts correctly correlate into incidents,
duplicate/suppressed alerts are handled (including retried webhook
deliveries deduping correctly at both the command and database level),
notifications fire, and the whole path is covered by tests that
specifically exercise the race conditions identified in
`review/critical-review.md` §2 (concurrent alert correlation, duplicate
delivery).

## Phase 2 — Evidence and read-only investigation plumbing (still no LLM)

- `evidence-service`: adapters for Prometheus and Loki first (the two
  needed for the MVP correlation/verification loop), `live`/`record`/
  `replay` modes, immutable evidence storage with content hashing.
- Wire `incident-core`'s verification path to `evidence-service` using a
  hand-authored (not agent-proposed) test remediation, to prove the
  execute → verify → resolve loop end-to-end before the agent exists.

*Exit criteria*: a manually-triggered "fake remediation" for a synthetic
incident goes through `REMEDIATION_IN_PROGRESS` → `VERIFYING` →
`RESOLVED`/`VERIFICATION_FAILED` correctly, entirely without any model
call — this proves the state machine's remediation/verification wiring
independent of agent correctness.

## Phase 3 — Policy engine and action catalog

- `policy-engine`: the pure `evaluate()` function and rule DSL, the initial
  policy version.
- `incident-core`: the `PolicyEvaluationContext` builder (kill-switch table
  reads, remediation-rate queries, incident field lookups) that runs
  immediately before every `policy-engine` call; the `kill_switches` table
  and its admin-only write path.
- `action_catalog` v1 with 2–3 real, low-risk actions (e.g.
  `restart_deployment` against the `kind` cluster).
- `remediation-executor`: Kubernetes adapter for the initial catalog
  entries, scoped credentials, idempotency-key-aware execution.
- Full adversarial policy-safety test suite (ADR-0007's "must always deny"
  cases).

*Exit criteria*: a manually-created `RemediationProposal` (still not
agent-generated) correctly flows through policy → approval → execution →
verification against the `kind` cluster, including the deny/kill-switch/
rate-limit paths.

## Phase 4 — Approval UI

- `apps/web` (Next.js): incident list/detail, RCA/evidence viewer,
  approval flow.
- `incident-core` public read/command API for the UI (BFF pattern).
- RBAC for approvals.

*Exit criteria*: a human can see a manually-created incident/proposal in
the UI and approve/deny it, with the decision correctly enforced by
`incident-core`.

## Phase 5 — Investigation agent (first LLM involvement)

- `investigation-agent`: tool-use loop against Claude, all tools proxying
  `evidence-service`, `submit_findings` schema validation, budgets.
- `incident-core`'s evidence-citation and action-catalog validation on
  investigation results (§07), including the `selected_root_cause_index` /
  `inconclusive_reason` mutual-exclusivity check that routes the incident
  to `RCA_READY` or directly to `ESCALATED` (§04).
- Initial small golden dataset (even 10–20 hand-built fixtures) and
  `eval-harness` skeleton running against it in replay mode, gating CI for
  this service specifically from the start — not bolted on later.

*Exit criteria*: for the golden dataset, the agent produces schema-valid,
evidence-grounded hypotheses; policy-safety and schema-validation tests are
green in CI on every change to this service.

## Phase 6 — Full autonomous loop, in shadow mode

- Wire `investigation-agent` into the real `INVESTIGATING` state
  transition, but run remediation proposals in **shadow mode**: generate
  and log the proposal and its policy decision, but never execute — always
  route to a human-visible "would have done X" record instead of
  `AWAITING_APPROVAL`/execution.
- Use this period specifically to grow the golden dataset from real
  incidents (per `review/critical-review.md` §8's recommendation),
  human-reviewed before being added.

*Exit criteria*: some number of real production incidents (a threshold the
team sets, e.g. 20–30) have gone through shadow-mode investigation with
acceptable groundedness/precision as scored by the eval harness, and the
resulting fixtures have been added to the golden dataset.

## Phase 7 — Live autonomous remediation, narrow scope first

- Turn off shadow mode for a small, deliberately chosen subset of
  low-blast-radius actions/environments (e.g. staging-only, or a single
  well-understood prod action) via a reviewed policy version.
- Full verification and rollback paths live.
- Expand the action catalog and the set of environments/actions eligible
  for `ALLOW`-without-approval only via subsequent, separately reviewed
  policy changes, backed by the accumulating verification track record.

*Exit criteria*: the platform is doing what the task describes, end to
end, for a narrow, intentionally chosen initial scope — with every
mechanism in `review/critical-review.md`'s follow-up list addressed before
this phase begins, not deferred past it.

## Ongoing, starting no later than Phase 1

- `eval-harness` and its golden dataset grow throughout, not just in
  Phase 5+ — Phase 1–4's deterministic components get their own
  correctness test suites from day one; the LLM-specific eval harness is
  additive on top, not a replacement for testing the deterministic core.
- Security review of credential scoping and network policy for each
  service as it's introduced, not as a single pass at the end.
