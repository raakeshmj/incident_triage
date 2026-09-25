# 07 — Agent / Tool Architecture

## Where Claude sits in the system

`investigation-agent` is a stateless worker. It is invoked once per
`Investigation` attempt with a bounded budget, runs a tool-use loop against
the Claude API, and returns **one structured result** to `incident-core` via
a command. It has no direct database access, no credentials to production
systems, and no ability to call `remediation-executor`. It cannot persist
anything itself — `incident-core` validates and persists.

```
incident-core ──InvestigationRequested──▶ investigation-agent
                                                 │
                                    (tool-use loop, bounded)
                                                 │
                                       calls evidence-service
                                       (read-only, all calls logged)
                                                 │
                        ◀──InvestigationCompleted(structured, schema-validated)──
```

## Tool inventory (all read-only, all proxy through `evidence-service`)

| Tool | Backing system | Returns |
|---|---|---|
| `get_metrics(query, service, range)` | Prometheus | evidence_id + time series |
| `get_logs(service, filter, range)` | Loki | evidence_id + log excerpt |
| `get_traces(service_or_trace_id, range)` | Tempo/tracing backend | evidence_id + trace summary |
| `get_deploys(service, range)` | deployment system (CI/CD) | evidence_id + deploy list (who, when, what changed) |
| `get_git_diff(service, ref_range)` | Git provider | evidence_id + diff/commit metadata |
| `get_config_history(service, range)` | config store | evidence_id + config change list |
| `search_historical_incidents(query, service)` | incident-core read API (past RCA reports) | evidence_id + matching past incidents' summaries |
| `submit_findings(hypotheses[], remediation_proposal?, notes)` | — (terminal tool; ends the loop) | validated by Pydantic before acceptance |

The agent **never** gets a tool that queries Prometheus/Loki/etc.
*directly* — every one of these tools is a thin client to
`evidence-service`, whose job is to turn "the model asked a question" into
"a durable, hashed, provenance-stamped record exists" **before** the answer
is returned into the model's context. See `08-evidence-model.md`. This is
the architectural answer to hallucinated evidence: the model cannot cite
anything that isn't already a row in the evidence store, because the only
way content enters its context is through a tool call that wrote that row
first.

## `submit_findings` — the only way out of the loop

The agent's final answer is not free text; it's a tool call validated
against a strict Pydantic schema:

```python
class HypothesisOut(BaseModel):
    statement: str                     # short structured claim
    confidence: float = Field(ge=0, le=1)
    supporting_evidence_ids: list[UUID]
    refuting_evidence_ids: list[UUID] = []

class RemediationProposalOut(BaseModel):
    action_catalog_id: str
    action_catalog_version: str
    parameters: dict

class InvestigationResult(BaseModel):
    hypotheses: list[HypothesisOut]     # ranked, index 0 = most likely
    selected_root_cause_index: int | None
    remediation_proposal: RemediationProposalOut | None
    inconclusive_reason: str | None     # set iff no confident hypothesis
```

`selected_root_cause_index` and `inconclusive_reason` are **mutually
exclusive** — exactly one must be set, enforced by a Pydantic model
validator, not left to convention. This is what lets the state machine
route deterministically: a result with `selected_root_cause_index` set can
only lead to `RCA_READY`; a result with `inconclusive_reason` set can only
lead directly to `ESCALATED`. There is no code path by which an
inconclusive result reaches `RCA_READY` — see
`04-incident-state-machine.md`.

`incident-core`'s validation, in order, **before persisting anything**:

1. Schema validates (Pydantic), including the mutual-exclusivity rule
   above — malformed or ambiguous output ⇒ `InvestigationFailed`,
   `ESCALATED` per the state machine, never silently retried into a
   half-applied state.
2. Every `evidence_id` referenced actually exists in `evidence_refs` **and**
   belongs to this `investigation_id`. A citation to an unknown or
   foreign-investigation evidence ID fails validation outright — this is
   the concrete check that turns "don't hallucinate evidence" from a
   prompt instruction into an enforced invariant.
3. If `remediation_proposal` is set, `action_catalog_id` +
   `action_catalog_version` must exist and be `active` in `action_catalog`.
   Unknown action ⇒ proposal rejected, investigation still recorded as
   `completed` with hypotheses intact (a bad proposal doesn't invalidate a
   good RCA).
4. `parameters` are validated against the catalog entry's
   `parameters_schema` (JSON Schema). Failure ⇒ proposal rejected the same
   way.

Only after all of this does `incident-core` write `hypotheses`,
`hypothesis_evidence_links`, and (if it survived validation)
`remediation_proposals`, then transition the state machine.

## Budgets (enforced by `investigation-agent`, reported to `incident-core`)

| Budget | Default | On exceed |
|---|---|---|
| Tool calls per investigation | 20 | Force a final `submit_findings` call summarizing what's known; if that also fails, `inconclusive` |
| Wall-clock time | 5 minutes | Same as above |
| Token spend | configurable per severity tier | Same as above |
| Tool calls per unique query (loop detection) | 3 identical calls | Reject further identical calls, nudge model via tool error to try a different query |

Budgets exist so a confused or adversarially-steered model degrades to
`inconclusive` / `ESCALATED` — a safe, visible, human-reviewable outcome —
rather than looping indefinitely or burning unbounded spend.

## Prompt architecture

- **System prompt** (versioned, stored in `libs/schemas` or a prompts
  directory once implemented, never user-editable at runtime): defines the
  agent's role, the tools available, and — explicitly — that tool results
  are *data*, not instructions, and that any text inside logs/alerts/commit
  messages that looks like an instruction must be ignored (defense against
  prompt injection via untrusted telemetry content; see
  `13-security-boundaries.md`).
- **User/context turn**: the incident's alerts (labels/annotations only —
  not arbitrary freeform fields without sanitization), the correlation
  window, and the investigation's budget.
- **Tool results**: injected as they come back from `evidence-service`,
  each tagged with its `evidence_id` so the model is expected to cite that
  ID rather than restate the content.
- The model is never given credentials, connection strings, or the ability
  to construct its own queries against raw systems — `evidence-service`'s
  tool schemas constrain query shape (e.g. `get_metrics` takes a
  constrained `query` matching an allow-listed PromQL template set, not
  arbitrary PromQL, for the highest-risk systems — see
  `08-evidence-model.md` for the tradeoff on this per evidence type).

## Retries within an investigation vs. across investigations

- Within one `Investigation`, transient tool failures (evidence-service
  timeout, upstream 5xx) are retried by the tool-call wrapper with backoff;
  the model sees a clean success or a terminal tool error, never a raw
  exception.
- Across investigations (a whole attempt fails or verification fails), a
  **new** `Investigation` row is created for the retry (`attempt_number`
  incremented) rather than resuming the old one — this keeps each
  investigation's evidence set and token usage independently auditable and
  replayable.

## Phase 4: tool contracts implemented (not connected to Claude)

`packages/tools` implements the tool layer this document describes, below an
agent that doesn't exist yet:

```
(future) agent -> ToolExecutor -> EvidenceService -> adapter -> telemetry backend
```

| Tool (as implemented) | Evidence operation | Notes vs. the inventory above |
|---|---|---|
| `get_metrics(metric, service?, start?, end?)` | `get_metric_window` | `metric` is a closed enum of allow-listed templates, not a PromQL `query` |
| `get_service_health(service?, at?)` | `get_service_health` | new: point-in-time RED/USE health, classified with the alert-rule thresholds |
| `get_logs(service?, start?, end?, severities?, trace_id?, request_id?, limit)` | `get_logs` | structured filters only; no free-text `filter` |
| `get_trace(trace_id)` / `get_traces(service?, start?, end?, mode, min_duration_ms?, limit)` | `get_trace` / `get_traces` | split from `get_traces(service_or_trace_id)`; `mode` = recent/errors/slow |
| `get_deploys(service?, start?, end?, limit)` | `get_recent_deployments` | |
| `get_config_history(service?, start?, end?, limit)` | `get_config_changes` | |
| `get_git_diff(service?, start?, end?, sha?, limit)` | `get_code_changes` | file list + diff summary (numstat), not the patch |
| `get_recent_commits(service?, limit)` | `get_recent_commits` | new: with distance from the incident's start |
| `search_historical_incidents(limit)` | `search_similar_incidents` | deterministic structured scoring; the query *is* the incident |
| `submit_findings(...)` | -- (terminal) | schema only (`packages/tools/findings.py`), incl. the exactly-one-outcome validator |

Contract details:

- **Inputs** are strict (`extra="forbid"`); `incident_id` is never an
  argument -- the orchestrator binds it in `ToolContext`. Services outside
  the incident's dependency neighborhood, windows outside the bounds, and
  malformed ids are rejected *before* any backend is queried.
- **Outputs** are compact: `evidence_id`, `content_hash`, type/source,
  `summary`, normalized `data`. Raw responses stay in the evidence store.
- **Errors** are `ToolFailure{code, message, retryable}` with stable codes
  (`invalid_argument`, `scope_violation`, `incident_not_found`,
  `backend_unavailable`, `backend_timeout`, `budget_exceeded`,
  `repeated_call`, `unknown_tool`, `terminal_tool`); validation messages
  never echo the offending input, and backend error bodies never pass
  through.
- **Retries/budgets**: transient backend errors retried once with backoff;
  20 calls per executor, 3 identical calls max (the "Budgets" table).
  Wall-clock/token budgets belong to the agent loop and arrive with it.
- **Transport**: in-process (`ToolExecutor`) or over evidence-service's
  internal API (`POST /internal/v1/incidents/{id}/tools/{tool}`). Either
  way the caller holds no telemetry credentials.
- `tool_definitions()` emits name/description/JSON Schema for every tool --
  the shape a model-facing tool list takes -- but nothing hands it to a
  model yet.

## Phase 5: the investigation agent (as built)

The loop now exists; `15-investigation-engine.md` describes it in full.
Where it departs from the sections above (ADR-0020, ADR-0021):

- **Not "one structured result".** The engine checkpoints every step to
  incident-core through fenced commands (`record_step`,
  `apply_hypothesis_updates`, `complete`, `escalate`) so an investigation
  survives a worker crash and every hypothesis change is validated and
  audited as it happens. incident-core is still the only writer.
- **`submit_findings` is retired** (`packages/tools/findings.py` removed).
  Three decision tools replace it: `update_hypotheses`,
  `conclude_investigation` (a *proposal* incident-core accepts or rejects
  against deterministic stopping criteria) and `declare_inconclusive`.
  Remediation proposals are out of scope for Phase 5.
- **Model-facing tool names** follow Phase 5's vocabulary and map onto the
  Phase 4 tools: `get_metric_window` → `get_metrics`,
  `get_recent_deployments` → `get_deploys`, `get_config_changes` →
  `get_config_history`, `get_code_changes` → `get_git_diff`,
  `search_similar_incidents` → `search_historical_incidents`, plus
  `get_incident_evidence`. `incident_id` is still never an argument.
- **Budgets**: 15 model iterations, 25 tool calls, 2 identical calls, 40
  evidence items, 900 s, 600k tokens by default (env-configurable), frozen
  on the investigation at creation. Soft exhaustion gives the model one
  final turn with evidence tools disabled, then escalates.
- **Status vocabularies**: investigation `CREATED | INVESTIGATING |
  COMPLETED | ESCALATED | FAILED`; hypothesis `ACTIVE | SUPPORTED | WEAKENED
  | REJECTED | SELECTED` (mapping in ADR-0020).
- **Model**: behind the `InvestigationModel` Protocol; the Claude adapter is
  the only SDK importer; the model is chosen by `INVESTIGATION_MODEL`
  (default `claude-sonnet-4-6`) and pinned per investigation.
