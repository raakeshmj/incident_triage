# 13 — Security Boundaries

## Trust zones

```
Zone 0 — Public Internet
    Alert sources (Alertmanager, PagerDuty, generic webhooks)
        │  HMAC-signed / shared-secret webhook auth, rate limiting, strict schema validation
        ▼
Zone 1 — Public Edge
    alert-ingestion
        │  internal network only, mTLS or service-mesh policy from here inward
        ▼
Zone 2 — Internal Core (trusted, no external network egress needed)
    incident-core, policy-engine, notification-service, web-ui BFF calls
        │
        ├─▶ Zone 3a — Model Boundary (outbound to Anthropic API only)
        │       investigation-agent
        │
        ├─▶ Zone 3b — Observability Egress (read-only credentials to observability/Git/history)
        │       evidence-service ──▶ Prometheus / Loki / Tempo / GitHub / config store / incident history
        │
        └─▶ Zone 4 — Privileged Execution (write credentials to production systems)
                remediation-executor ──▶ Kubernetes / CI-CD / feature-flag system
```

The single most important boundary is **Zone 3a/3b vs. Zone 4**:
nothing that can be influenced by model output (directly or via untrusted
telemetry content flowing through the model) holds write credentials to
production. `investigation-agent` and `evidence-service` are read-only,
full stop.

## Network policy (Kubernetes)

| Service | Ingress from | Egress to |
|---|---|---|
| `alert-ingestion` | Public internet (via ingress controller) | `incident-core` only |
| `incident-core` | `alert-ingestion`, `investigation-agent`, `remediation-executor`, `web-ui` BFF, `policy-engine` | Postgres, Redis, `policy-engine` |
| `investigation-agent` | `incident-core` (via Redis Stream command) | `evidence-service`, Anthropic API (HTTPS egress allowlist) |
| `evidence-service` | `investigation-agent`, `incident-core` (verification checks) | Prometheus, Loki, Tempo, GitHub API, k8s API (read-only), its own evidence store |
| `policy-engine` | `incident-core` | none (pure computation) |
| `remediation-executor` | `incident-core` only | Kubernetes API (scoped), CI/CD API (scoped), feature-flag API (scoped) — explicit egress allowlist, nothing else |
| `notification-service` | `incident-core` events (Redis Stream) | Slack/email/webhook endpoints |
| `web-ui` | Public internet (authenticated) | `incident-core` public API only |

Each service gets its own Kubernetes namespace-scoped NetworkPolicy;
`remediation-executor`'s is the most restrictive and the most carefully
reviewed, since it's the only component that can change production state.

## Credentials

- Every service's credentials are scoped to exactly what its table in
  `02-component-boundaries.md` says it may touch — no shared "god"
  service account.
- `remediation-executor` uses **short-lived, workload-identity-based**
  credentials per action type (e.g. a Kubernetes ServiceAccount token
  bound to a specific namespace and verb set, refreshed per invocation
  rather than a long-lived static key) — see `09-remediation-policy-boundaries.md`.
- `evidence-service`'s Git/observability credentials are read-only at the
  IAM level, not just "the code happens not to write" — defense in depth
  against a bug or compromise in that service.
- **Database credentials**: `incident-core` and `evidence-service` each
  hold a distinct Postgres role granted privileges only on their own
  logical schema (`incident_core`, `evidence`) within the shared v1
  instance — see `06-database-design.md` and ADR-0013. This turns the
  single-writer ownership rule (`02-component-boundaries.md`) into a
  database-enforced guarantee rather than only an application-level
  convention: a bug in one service's code cannot write the other's tables,
  because its role has no grant to do so.
- Secrets are managed via the platform's secret manager (e.g. k8s Secrets
  backed by an external secret store — Vault or cloud KMS-backed) and
  injected at runtime; nothing is baked into images or committed to the
  repo. `.env.example` documents required variables without values.

## Prompt-injection defense

Log lines, alert annotations, commit messages, and config values are
**attacker- or at-least-untrusted-input-adjacent content** that flows into
the model's context via evidence. Mitigations:

1. The system prompt explicitly frames all tool results as data to reason
   about, never as instructions to follow (`07-agent-tool-architecture.md`).
2. The model's only channel of effect on the world is the strictly-typed
   decision tools (`update_hypotheses`, `conclude_investigation`,
   `declare_inconclusive` since Phase 5; originally `submit_findings`) — even a fully "convinced" model can only
   produce a `RemediationProposalOut` referencing an existing
   `action_catalog_id`; it cannot emit a shell command, a URL to fetch, or
   arbitrary instructions that any downstream component would execute.
3. `evidence-service` truncates and strips control sequences from raw
   payloads before they're summarized into tool results (defense against
   e.g. terminal escape sequences or absurdly long injected strings
   designed to dominate context).
4. Tool query surfaces are constrained (allow-listed PromQL templates,
   scoped LogQL) so even a manipulated model can't be steered into using a
   legitimate tool to exfiltrate data it shouldn't have access to (e.g.
   querying an unrelated service's logs) — parameters are validated against
   the incident's own service/environment scope server-side in
   `evidence-service`, not trusted from the model's arguments.

## RBAC (human-facing)

- Roles: `viewer`, `on_call_engineer`, `service_owner`, `platform_admin`.
- Approval authority is role- and blast-radius-tier-gated
  (`policy_decisions.required_approver_roles`); `incident-core` checks the
  authenticated user's role at decision time.
- `platform_admin` is the only role that can edit `policies` and
  `action_catalog` — and even then, only via the normal CI/CD path (PR
  review + merge triggers a deploy), never a runtime "edit policy" API
  endpoint. There is deliberately no such endpoint.
- The global kill switch is a `platform_admin`-only, heavily audited
  action, available even when other systems are degraded (a simple
  feature-flag style toggle, not dependent on the full pipeline being
  healthy).

## Audit

- The `events` outbox table is the audit log: append-only, every state
  transition, every policy decision, every approval, every execution
  result. Never deleted. Optionally streamed to an external SIEM via the
  same Redis Stream fan-out `notification-service` uses.
- `policy_decisions` and `executions` are the two tables most likely to
  matter for a security or compliance review; both are immutable and
  timestamped with the exact policy/action-catalog version used.

## Phase 5: the investigation agent's trust boundary

- The agent is read-only: its tools are the evidence tools and three
  decision tools whose effects incident-core validates. No tool mutates
  production, and there is no DB/Redis/shell/HTTP/Git/Kubernetes tool.
- Authorization is outside the model: `incident_id`/`investigation_id` are
  bound in `ToolContext` by the engine, never accepted as arguments; scope
  (the incident's service neighbourhood, time bounds) is checked by
  evidence-service; the tool list is fixed in code and cannot be extended
  by model output.
- The incident context is built by the application from stored rows;
  alert text is sanitized and presented as data. Model output never feeds
  back into the context except as its own prior turns.
- Evidence ids the model cites must be ones it was shown for this
  incident; anything else is rejected and recorded.
- `ANTHROPIC_API_KEY` is read only by the SDK in the worker; it is never
  logged, never in a tool result, never persisted. Logs carry counts,
  names and codes, not prompts, payloads or model text (those live in the
  access-controlled `investigation_steps` table).
- Boundary tests: `packages/agents` imports no DB driver, Redis, HTTP
  client or `subprocess`; only `packages/agents/claude.py` imports
  `anthropic`.
