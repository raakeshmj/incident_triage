# Operations: running the whole loop locally

Everything below runs against the local stack (`make infra-up` or
`make infra-up-full`). Nothing here touches a real production system:
the remediation executor acts only on the simulator's control plane.

## Processes

| Process | Command | Does |
|---|---|---|
| API | `make run-api` | alert ingestion, remediation/approval endpoints, read API for the dashboard (`/api/v1/incidents`, `/detail`, `/overview`, `/metrics`, `/evidence/{id}`) |
| Outbox relay | `make run-worker` | Postgres outbox → Redis Streams (at-least-once) |
| Consumer | `make run-consumer` | generic consumers, DLQ |
| Investigation worker | `make run-investigation-worker` | debounce scheduler (TRIAGING and VERIFICATION_FAILED → INVESTIGATING), investigations, stale-lease resume |
| Remediation worker | `make run-remediation-worker` | planning on InvestigationCompleted, execution of approved remediations (baseline first), approval timeouts |
| Verification worker | `make run-verification-worker` | VerificationRequested → start; ticks due verifications; finalizes verdicts |
| Dashboard | `make dashboard-dev` | operations console on http://localhost:5173 (proxies `/api`) |

Every worker writes a heartbeat (`heartbeat:<role>:<host>:<pid>`, 30 s TTL)
to Redis; the Operations page lists the live ones, the dead-letter stream
length and failed outbox events.

## Investigating without a model: `INVESTIGATION_PROVIDER=heuristic`

With no usable model credential, set `INVESTIGATION_PROVIDER=heuristic`
for the investigation worker. It then runs the evaluation harness's
deterministic, rule-based investigator: no API call, no credential, not AI.
It exists to exercise the lifecycle locally; it is not a production
investigator. The default remains the configured model provider.

## Shortening verification windows: `VERIFICATION_TIME_SCALE`

The catalog's windows are minutes long (`architecture/10-verification-design.md`).
For a local demo set `VERIFICATION_TIME_SCALE` (e.g. `0.2`) for the
**remediation worker** — the scaled spec is frozen into each verification
when it is created. Keep `1.0` anywhere that matters.

## A manual lifecycle run (bad deployment)

```bash
make infra-up-full && make migrate
export OPERATOR_API_TOKEN=... REMEDIATION_APPROVERS='alice=service_owner|on_call_engineer'
make run-api & make run-worker &
INVESTIGATION_PROVIDER=heuristic make run-investigation-worker &
VERIFICATION_TIME_SCALE=0.2 make run-remediation-worker &
make run-verification-worker &
python -m simulator.chaos.cli start bad-deployment --service checkout-service --duration 900
```

1. Prometheus fires `HighErrorRate` → Alertmanager → API → incident TRIAGING.
2. After the debounce the investigation worker investigates → RCA_READY.
3. The remediation worker plans `rollback_deployment` → policy →
   AWAITING_APPROVAL. Approve it in the dashboard (incident detail →
   "Review and decide", with the operator token) or via
   `POST /api/v1/remediations/{id}/approval` with the proposal hash and
   policy decision id.
4. The runner captures the baseline, rolls back, and requests verification
   → VERIFYING.
5. The verification worker observes through the evidence service until N
   consecutive passing observations → RESOLVED; check it in the dashboard,
   `GET /api/v1/incidents/{id}/detail`, and `incident_core.verifications`.

If the service keeps failing, verification FAILS and the incident goes to
VERIFICATION_FAILED → a new investigation (attempts permitting) or
ESCALATED. Nothing is re-executed automatically.

## Demo data

`make seed-demo` adds incidents in every lifecycle state to the dev
database, by running golden scenarios through the real lifecycle against
scenario worlds (no simulator). It adds; it never wipes.

## Kill switches

`PUT /api/v1/kill-switches/{global|service:<name>}` (operator token) denies
every new proposal and stops every execution that has not started.

## Pitfalls seen in the Phase 8 manual run

- **Heuristic investigator right after `infra-up-full`.** The change
  registries are seeded at stack start (including a config record
  `None → v1`). For the next 30 minutes that seed falls inside the
  heuristic's change look-back, so a bad deployment injected immediately
  can be attributed to configuration. Wait 30 minutes after seeding, or use
  a real model. (The planner no longer proposes reverting a change that had
  no previous value; it escalates instead.)
- **Chaos that expires on its own** does not record a rollback in the
  deployment registry (only `chaos.cli stop` does), so the registry keeps
  reporting the bad version. Stop scenarios explicitly.
- **Test fixtures truncate the dev database.** Running the integration /
  e2e suites during a manual run deletes its incidents, and Alertmanager
  will not re-send an alert episode it already delivered (1 h
  `repeat_interval`). Recreate the Alertmanager container, or let the
  episode resolve, before re-injecting.
- **Verification windows vs. metric windows.** Error rates come from
  multi-minute Prometheus rates; with a small `VERIFICATION_TIME_SCALE`
  the window may close before the rate reflects recovery, and verification
  correctly FAILS. 0.25 was just enough in the manual run (PASSED on the
  9th observation).

## Runtime model configuration

The investigation engine depends only on the `InvestigationModel`
interface; the provider is configuration. Three providers ship:

| `INVESTIGATION_PROVIDER` | Adapter | Configure |
|---|---|---|
| `anthropic` | `packages/agents/claude.py` (Messages API, explicit prompt caching) | `ANTHROPIC_API_KEY`, `INVESTIGATION_MODEL` |
| `openai_compatible` | `packages/agents/openai_compatible.py` (Chat Completions) | `INVESTIGATION_BASE_URL`, `INVESTIGATION_API_KEY`, `INVESTIGATION_MODEL` |
| `openrouter` | preset of the generic adapter (fixed base URL) | `OPENROUTER_API_KEY`, `INVESTIGATION_MODEL` |

### Any OpenAI-compatible endpoint

The generic provider is meant for APIs that implement OpenAI Chat
Completions with function tools (OpenAI, Groq, Together, vLLM, Ollama,
gateways such as OpenRouter or APInex). Changing provider or model means
changing these three values -- no code:

```bash
INVESTIGATION_PROVIDER=openai_compatible
INVESTIGATION_BASE_URL=https://api.example.com/v1   # <base>/chat/completions is called
INVESTIGATION_API_KEY=...                            # .env only; never printed, persisted or traced
INVESTIGATION_MODEL=<model id exactly as the provider lists it>
```

The base URL is persisted with each investigation (so a resumed run reaches
the same endpoint) and is therefore refused if it carries credentials, a
query string, or plain http to a non-local host. The key is read at call
time and is never part of the persisted model settings.

Optional capabilities -- declare only what the endpoint supports; anything
undeclared is not sent, and the engine's own validation applies regardless:

| Setting | Default | Effect |
|---|---|---|
| `INVESTIGATION_TOOL_CHOICE` | `true` | send `tool_choice: "auto"` |
| `INVESTIGATION_PARALLEL_TOOL_CALLS` | unset | send `parallel_tool_calls` when set |
| `INVESTIGATION_STRICT_TOOLS` | `false` | `strict: true` on tools whose schema is strict-compatible |
| `INVESTIGATION_REASONING` | `none` | `reasoning_effort` (OpenAI style) or `reasoning_object` (OpenRouter style) carries `INVESTIGATION_EFFORT`; `none` never sends it |
| `INVESTIGATION_MAX_TOKENS_PARAM` | `max_tokens` | or `max_completion_tokens` |
| `INVESTIGATION_CONTEXT_TOKENS` | unset | the context window, recorded with the model settings |
| `INVESTIGATION_PROVIDER_PROMPT_CACHING` | `false` | the provider caches automatically; cached tokens are recorded |

Behaviour, whatever the endpoint:

- one request per decision; retries and budgets are the engine's
  (`INVESTIGATION_MODEL_ATTEMPTS`, `INVESTIGATION_RETRY_BACKOFF_SECONDS`,
  `INVESTIGATION_API_TIMEOUT_SECONDS`). Terminal errors (authentication,
  model_unavailable, bad_request) are not retried;
- errors are normalized to authentication / rate_limit / model_unavailable /
  bad_request / timeout / upstream_error / malformed_response, with bounded
  diagnostics that are scrubbed of keys and bearer tokens;
- tool calls come from the structured `tool_calls` field; malformed JSON is
  flagged for the engine to reject; truncated (`length`) or failed
  (`error`) generations never become actions;
- hidden reasoning is never stored (reasoning fields dropped, inline
  `<think>` blocks stripped); only reasoning token counts are kept;
- no prompt-cache hints are sent (OpenAI-compatible APIs cache
  automatically or not at all); cached tokens a provider reports are recorded.

Check an endpoint before an investigation (tiny requests, no project data):

```bash
python scripts/provider_check.py    # catalog id, one completion, one trivial tool call
```

### OpenRouter

The `openrouter` preset uses `https://openrouter.ai/api/v1` and
`OPENROUTER_API_KEY`; everything else is the generic adapter.

- Free models share upstream pools. In the first validation (2026-09-27)
  `qwen/qwen3.8-27b:free` was rate-limited upstream for every attempt, and
  `dots-studio/dots-3-note-preview:free` failed upstream on long
  investigation contexts (about 25–30k prompt tokens) at the conclusion
  turn. Neither produced an RCA; both escalated correctly. Reliable live
  runs need a provider/model with dedicated capacity.
