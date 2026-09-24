# ADR-0008: Remediation is expressed only through a human-authored Action Catalog

Status: Accepted

## Context

"Autonomous remediation" could mean anything from "run an arbitrary
shell command the model wrote" to "invoke one of a small number of
pre-reviewed, parameterized operations." The former is not something a
production system can responsibly allow given an LLM's failure modes.

## Decision

Remediation is always the invocation of a versioned `action_catalog` entry
— a human-authored, code-reviewed, parameterized operation (e.g.
`restart_deployment`, `rollback_deployment`, `scale_replica`,
`toggle_feature_flag`) with a JSON-Schema-validated parameter set, a
declared blast-radius tier, and declared success criteria for
verification. The model's remediation output is exactly
`{action_catalog_id, action_catalog_version, parameters}` — never a
command string, script, or code.

## Alternatives considered

- **Model-generated scripts/commands executed by the executor**: rejected
  — turns the executor into an arbitrary-code-execution engine gated only
  by the model's judgment. Incompatible with the core rule that the LLM
  must never bypass deterministic controls.
- **A large, generic "kubectl proxy" action with free-form arguments**:
  rejected — free-form arguments reintroduce the same problem in a
  smaller box; blast radius and parameter validation both depend on the
  action being specific enough that its `parameters_schema` can actually
  constrain what happens.

## Consequences

Every possible remediation the system can ever take is enumerable, human
-reviewed, and testable ahead of time (including in the eval harness's
dry-run mode). Adding a new capability is a deliberate, reviewed act (a
new catalog entry + adapter), not something the model can invent at
runtime. The cost: the system can only remediate what's in the catalog —
novel situations requiring a genuinely new action always escalate to a
human rather than being handled autonomously. This is treated as a
feature, not a limitation to work around.
