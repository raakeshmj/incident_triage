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

`incident-core`'s validation, in order, **before persisting anything**:

1. Schema validates (Pydantic) — malformed output ⇒ `InvestigationFailed`,
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
