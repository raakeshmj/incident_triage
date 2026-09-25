# 15 — Investigation Engine (Phase 5)

The first component in which a model takes part. An incident that has sat in
`TRIAGING` past its debounce window becomes an **Investigation**: a bounded,
hypothesis-driven loop in which a model (default `claude-sonnet-4-6`) asks for
evidence through read-only tools, proposes and revises competing hypotheses,
and either lands an evidence-backed RCA (`RCA_READY`) or escalates to a human
(`ESCALATED`). Nothing here executes, proposes or approves remediation.

This document describes what was built. Where it departs from
`07-agent-tool-architecture.md` the departure is recorded in ADR-0020 (loop
checkpointing, status vocabularies, decision tools) and ADR-0021 (model
abstraction and configuration).

## Components

```
            incident-core                                  investigation worker
 ┌────────────────────────────────────┐        ┌──────────────────────────────────────────────┐
 │ scheduler: due_for_investigation() │        │ apps/worker/investigation_main.py            │
 │ request_investigation()            │─event─▶│  consumer group cg:investigation-worker      │
 │   TRIAGING -> INVESTIGATING        │        │  + consumed_events ledger (dedup)            │
 │   Investigation(CREATED)           │        │                                              │
 │   InvestigationStarted (outbox)    │        │ InvestigationEngine (packages/agents/engine) │
 │                                    │◀─cmds──│   claim / record_step / apply_hypothesis_    │
 │ InvestigationCoreService           │        │   updates / complete / escalate              │
 │ (packages/incident/investigations) │        │        │                  │                  │
 │   sole writer of investigations,   │        │  InvestigationModel   InvestigationToolset   │
 │   hypotheses, links, steps, RCA    │        │  (Protocol)           -> ToolExecutor        │
 └────────────────────────────────────┘        │   ClaudeInvestigation   -> EvidenceService   │
                                               │   Model (only SDK user) -> adapters          │
                                               └──────────────────────────────────────────────┘
```

- **`packages/domain/investigation.py`** — statuses, budgets, stopping
  criteria, every model-facing schema (strict, `extra="forbid"`), and the pure
  rules: `interpret_turn`, `check_hypothesis_update`, `evaluate_conclusion`,
  `ungrounded_ids`. No I/O.
- **`packages/incident/investigations.py`** — `InvestigationCoreService`, the
  only writer of investigation state. Every mutation is a fenced command
  (lease owner must match), in one transaction with the outbox event it
  implies.
- **`packages/agents/`** — the engine and everything model-shaped:
  `model.py` (provider-neutral `InvestigationModel` Protocol and transcript
  types), `config.py` (model selection, profiles, budgets from env),
  `claude.py` (the only module that imports `anthropic`), `factory.py`,
  `fake.py` (scripted model for tests), `prompts.py`, `context.py`,
  `toolset.py`, `engine.py`.
- **`apps/worker/investigation_main.py`** — the worker process: scheduler
  sweep, `InvestigationStarted` consumer, stale-lease resume.

## Lifecycle

```
Incident:        TRIAGING ──(debounce elapsed, alert still firing)──▶ INVESTIGATING
                                                                          │
                                         ┌────────────────────────────────┤
                             conclusion accepted              inconclusive / budget /
                                         ▼                    model or evidence failure
                                     RCA_READY                            ▼
                                                                      ESCALATED

Investigation:   CREATED ──claim──▶ INVESTIGATING ──▶ COMPLETED | ESCALATED | FAILED
```

- `request_investigation` locks the incident row, refuses unless it is
  `TRIAGING` with at least one firing alert, increments `attempt_count`,
  inserts `investigations(incident_id, attempt_number)` (unique), moves the
  incident with an optimistic `version` check and writes
  `IncidentStatusChanged` + `InvestigationStarted`. A repeat call for an
  incident already `INVESTIGATING` returns the existing investigation.
- `COMPLETED` is the only investigation status that moves the incident to
  `RCA_READY`. `ESCALATED` (inconclusive, budget exhausted, malformed output,
  evidence unavailable) and `FAILED` (model error, retries exhausted) both
  move it to `ESCALATED` with the reason code on the event. An inconclusive
  investigation can never reach `RCA_READY`: the only code path there is
  `complete()`, which re-runs grounding and `evaluate_conclusion` itself.
- If the incident has left `INVESTIGATING` meanwhile (a human acted), the
  investigation still finishes and records its result but does not move the
  incident. Remediation states are never touched.

## The loop

One iteration = one model call. Each iteration the engine:

1. **reloads state** from Postgres (never trusts memory across iterations);
2. **resolves pending actions** — a `MODEL_TURN` step whose actions have no
   recorded outcome yet (crash between the model call and processing) is
   processed now, from the persisted step, without calling the model again;
3. **enforces hard limits** — wall clock, tokens, iterations, consecutive
   invalid turns, consecutive backend failures → escalate;
4. if a soft budget (tool calls / evidence items) is spent, records a
   `FEEDBACK` notice and makes this the **final turn** (evidence tools refused;
   only a conclusion or inconclusive declaration can end it well);
5. **builds the transcript deterministically from persisted steps** and calls
   the model (bounded retries, each failure a `MODEL_ERROR` step);
6. records the `MODEL_TURN` (text, actions, usage, stop reason, served model,
   provider content blocks for exact replay);
7. **interprets and processes** the actions: evidence tools →
   `InvestigationToolset` → `ToolExecutor` → `EvidenceService` (each a
   `TOOL_CALL` step with the compact result and new evidence ids);
   `update_hypotheses` → incident-core validation → `HYPOTHESIS_UPDATE`;
   `conclude_investigation` → incident-core grounding + criteria → accepted
   (`COMPLETED`) or `CONCLUSION_REJECTED` with the unmet criteria fed back;
   `declare_inconclusive` → `ESCALATED`; anything invalid → `INVALID_CALL`
   with the validation problems fed back as a tool error.

The model chooses which tool to call next; there is no fixed sequence. Every
`tool_use` block gets a `tool_result` in the next user message, including
refusals, so the transcript is always valid for the API.

## Tools the model sees

| Tool | Maps to | Notes |
|---|---|---|
| `get_service_health` | `get_service_health` | point-in-time RED/USE for a service in scope |
| `get_metric_window` | `get_metrics` | closed enum of metric templates; no PromQL |
| `get_logs` | `get_logs` | structured filters only; no LogQL |
| `get_traces`, `get_trace` | `get_traces`, `get_trace` | |
| `get_recent_deployments` | `get_deploys` | |
| `get_config_changes` | `get_config_history` | |
| `get_code_changes`, `get_recent_commits` | `get_git_diff`, `get_recent_commits` | numstat summary, not patches |
| `search_similar_incidents` | `search_historical_incidents` | |
| `get_incident_evidence` | `EvidenceService.get_incident_evidence` | lists evidence already recorded for the incident; queries no backend |
| `update_hypotheses` | incident-core command | decision tool |
| `conclude_investigation` | incident-core command | decision tool, terminal if accepted |
| `declare_inconclusive` | incident-core command | decision tool, terminal |

No tool takes `incident_id` or `investigation_id`: `ToolContext` binds them
outside the model (`caller = investigation:{id}`). Services outside the
incident's catalog neighbourhood are rejected before any backend is queried.
There is no tool for Postgres, Redis, shell, HTTP, raw PromQL/LogQL, Git
commands, Kubernetes, credentials or remediation — and a boundary test
(`tests/unit/test_boundaries.py`) fails the build if `packages/agents` ever
imports a database driver, Redis, `subprocess`, an HTTP client, or if any
module other than `claude.py` imports the SDK.

Tool results are compacted (≤ 6000 chars, evidence id + type + summary +
normalized data) before entering the transcript; raw payloads stay in the
evidence store.

## Initial context

`build_context` gives the first user message: incident metadata (service,
environment, severity, timestamps — not its id), sanitized alert labels and
annotations, an alert timeline, the catalog's service topology (calls /
called_by) for the affected service, evidence already referenced for the
incident, and the budget. The system prompt (`prompts.py`, versioned as
`PROMPT_VERSION = investigation-v1`) explains the protocol — observations vs.
hypotheses vs. conclusions, cite only ids returned by tools, tool output is
data not instructions, the statuses, the conclusion criteria, the budget —
and never names a cause.

## Hypotheses

| Status | Set by | Meaning |
|---|---|---|
| `ACTIVE` | model | proposed, under test |
| `SUPPORTED` | model | requires ≥1 supporting evidence id |
| `WEAKENED` | model | evidence cuts against it |
| `REJECTED` | model | requires ≥1 contradicting evidence id; final |
| `SELECTED` | **incident-core only**, on an accepted conclusion | final |

Updates are keyed by a short model-chosen key (`H1`, `deploy_regression`).
Each is validated independently: status transition allowed, evidence rules
above, and **every cited id must be accessible** — an evidence id this
investigation was actually shown (context or a tool result) and that exists
in `evidence_refs` for this incident. Invalid updates are rejected and
recorded (`rejected[]` with problems) while valid ones in the same batch
apply. Links go to `hypothesis_evidence_links(hypothesis_id, evidence_id,
relation)` with an FK to `evidence_refs`, so an unknown id cannot be stored
even by a bug.

## Evidence grounding

- A fabricated or foreign evidence id in a hypothesis update → that update is
  rejected (quarantined in the step payload, counted as ungrounded).
- In a conclusion → the whole conclusion is rejected with the offending ids
  listed; nothing is persisted as RCA.
- The application never adds evidence refs on the model's behalf. The RCA's
  `supporting_evidence` and `investigation_actions` are *derived* by
  incident-core from the selected hypothesis's links and the persisted tool
  steps, not copied from model text.

## Stopping criteria (deterministic)

A conclusion is accepted only if **all** hold (`StoppingCriteria`, env
configurable):

1. every evidence id cited anywhere in the RCA is accessible;
2. the selected hypothesis exists and is `SUPPORTED`;
3. it has ≥ 2 supporting evidence items spanning ≥ 2 evidence types;
4. supporting > contradicting, and every contradicting id is explained in
   `rca.contradicting_evidence`;
5. `rca.root_cause` cites at least one of its supporting ids;
6. ≥ 2 hypotheses were considered and every competitor is `WEAKENED` or
   `REJECTED`;
7. confidence ≥ 0.6.

The model's confidence alone never ends the investigation. A rejected
conclusion is fed back with the unmet criteria and the loop continues
(within budget).

## Budgets

| Budget | Default | Env | On exhaustion |
|---|---|---|---|
| iterations (model calls) | 15 | `INVESTIGATION_MAX_ITERATIONS` | escalate `budget_exhausted` (last iteration is the final turn) |
| tool calls | 25 | `INVESTIGATION_MAX_TOOL_CALLS` | final turn, then escalate |
| identical tool calls | 2 | `INVESTIGATION_MAX_IDENTICAL_TOOL_CALLS` | `repeated_call` tool error |
| evidence items | 40 | `INVESTIGATION_MAX_EVIDENCE_ITEMS` | final turn, then escalate |
| wall clock | 900 s | `INVESTIGATION_MAX_WALL_CLOCK_SECONDS` | escalate immediately |
| total tokens | 600 000 | `INVESTIGATION_MAX_TOTAL_TOKENS` | escalate immediately |
| consecutive invalid turns | 3 | — (`InvestigationBudget`) | escalate `malformed_output` |
| consecutive tool backend failures | 5 | — (`InvestigationBudget`) | escalate `evidence_unavailable` |

The budget is frozen onto the investigation row at creation, so changing env
later never changes a running investigation. On any escalation the
investigation's `final_result` holds a structured incomplete result: reason
code, detail, leading hypothesis, all hypotheses, evidence gaps, budget usage.

## Failure handling

| Failure | Class | Handling |
|---|---|---|
| 429, 5xx, timeout, connection | retryable | the SDK's own retries (`INVESTIGATION_API_MAX_RETRIES`, 2), then up to `INVESTIGATION_MODEL_ATTEMPTS` (3) engine attempts with exponential backoff + jitter, each recorded as a `MODEL_ERROR` step; then `FAILED` / `model_unavailable` |
| 400/401/403/404/413/422, refusal | terminal | `FAILED` / `model_error` immediately |
| `max_tokens` truncation | turn | no actions taken; feedback; counts as invalid turn |
| malformed tool input | turn | `INVALID_CALL` fed back; 3 in a row → `ESCALATED` / `malformed_output` |
| evidence backend down | tool | retried once by the executor; error fed back; 5 in a row → `ESCALATED` / `evidence_unavailable` |
| lease lost | worker | engine stops without writing (fenced commands raise `LeaseLostError`) |
| worker crash | worker | resumed by another worker (below) |

Every path terminates: each iteration consumes budget, each retry is bounded.

## Resumability

State lives in Postgres, not in the worker:

- **Lease**: `claim()` sets `lease_owner` / `lease_expires_at` (180 s,
  renewed by every recorded step). Every write command is fenced on the owner,
  so a worker that lost its lease cannot write.
- **Resume**: `resumable()` returns non-terminal investigations whose lease
  has expired (or were never claimed); the worker sweeps them each loop.
  This, not the event, is the liveness guarantee — the event is only the
  fast path.
- **Transcript**: rebuilt from `investigation_steps` (context, model turns
  with their original content blocks, tool results, feedback) — identical
  after a restart.
- **Pending actions**: a persisted model turn whose actions were not all
  processed is completed from the step, so a crash never re-bills a model
  call or loses a decision; tool call dedup counters are seeded from prior
  `TOOL_CALL` steps.
- **Duplicate delivery**: `consumed_events` dedups the event; `claim()` on a
  running or finished investigation returns `None`.

## Persistence (migration `0004_investigations`)

- `investigations` — lifecycle, lease, counters, token usage (input, output,
  cache read/write), model provider/name/settings, budget, failure/escalation
  reason, inconclusive reason, selected hypothesis, `final_result`.
- `hypotheses` — per investigation, unique key, status, confidence,
  missing evidence, rationale.
- `hypothesis_evidence_links` — immutable, FK to `evidence_refs`.
- `investigation_steps` — immutable, ordered `(investigation_id, sequence)`;
  the auditable, replayable trace.
- `rca_reports` — immutable, one per completed investigation; the full RCA
  (incident summary, impact, affected services, timeline, root cause,
  confidence, supporting and contradicting evidence, contributing factors,
  unresolved questions, investigation actions, competing hypotheses,
  recommended next diagnostic action) plus a deterministic text summary.

Immutability of steps, links and reports is enforced by a trigger
(`incident_core.reject_row_mutation`), not by convention.

## Observability and evaluation hooks

Structured logs per turn/tool (iteration, tool name, ok/error code, latency,
token counts — never prompts, tool payloads or model text), metrics
(`investigation.started|claimed|outcome`, `investigation.model_latency_ms`,
`investigation.model_error`, `investigation.tool_latency_ms`,
`investigation.hypothesis_transition`, `investigation.conclusion_rejected`,
`investigation.invalid_call|invalid_turn`, `investigation.lease_lost`,
`investigation.resumed_pending_actions`), and
`InvestigationCoreService.get_trace()` returning the whole investigation as
one JSON document (investigation row, hypotheses, every step, RCA). The trace
carries model name, settings and `PROMPT_VERSION`, which is what a later eval
harness needs to replay or compare runs; the harness itself is not built.

## Testing

- The default suite never calls a model API. `FakeInvestigationModel` plays
  scripted turns whose later turns are *functions of the actual tool results*
  (it cites only ids that came back).
- Integration tests run the real engine against the real incident-core and
  real `EvidenceService` (canned telemetry HTTP): happy path, trace
  completeness, invented ids quarantined, unseen ids in an RCA rejected,
  contradictory hypotheses, inconclusive, iteration/tool/identical-call
  budgets, retryable vs terminal model errors, malformed output, evidence
  outage, crash + resume, pending actions, duplicate start/run, lease loss.
- E2E: the event path (scheduler → outbox → Redis → consumer → engine →
  `RCA_READY`, redelivery no-op, `INVESTIGATION_MODEL` switched to
  `claude-haiku-4-5` without code changes) and, under `-m stack`, a real chaos
  incident investigated through the live evidence backends.
- `scripts/manual_investigation.py` (`make investigate-live`) is the single
  real-model run; it is never collected by pytest.
