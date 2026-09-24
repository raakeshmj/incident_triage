# ADR-0006: Agent output is schema-validated structured data, never freeform executable text

Status: Accepted

## Context

If the investigation agent's output were freeform text that some
downstream component parsed loosely (regex, "find the JSON in the
markdown," or worse, `eval`-adjacent interpretation), both correctness and
safety would depend on the model formatting its answer exactly right and
never being steered into producing something unexpected/adversarial.

## Decision

The agent's only path to producing a result is a single terminal tool call
(`submit_findings`) whose arguments are validated against a strict Pydantic
schema (`InvestigationResult` — see `architecture/07-agent-tool-architecture.md`)
using Claude's native structured tool-calling. Anything that fails schema
validation is treated as a failed investigation (`InvestigationFailed`),
never partially applied or "best-effort parsed."

## Alternatives considered

- **Freeform text output, parsed downstream**: rejected — fragile (any
  formatting drift breaks parsing) and unsafe (there's no schema boundary
  to reject an out-of-scope or malformed instruction before it reaches
  code that acts on it).
- **Let the model call `remediation-executor` directly as a tool**:
  rejected outright — this is the single change that would violate the
  system's core rule ("the LLM must never own system state or bypass
  deterministic controls"). A remediation proposal must pass through
  `policy-engine` and, by default, human approval; giving the model a tool
  that executes remediation removes both gates.

## Consequences

Malformed or out-of-schema model output fails loudly and safely
(`ESCALATED`, human review) instead of silently corrupting state or, worse,
being coerced into "close enough" execution. This does mean some genuinely
correct-but-oddly-phrased model answers get rejected and retried/escalated
— an accepted false-negative cost in exchange for eliminating an entire
class of unsafe-parsing failure modes.
