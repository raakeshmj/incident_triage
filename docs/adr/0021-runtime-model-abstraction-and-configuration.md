# ADR-0021: Runtime model abstraction and configuration

Status: Accepted

## Context

The investigation agent's model must be switchable (default
`claude-sonnet-4-6`; Haiku 4.5 and Opus 5.5 to be benchmarked later) without
changing application code, and the test suite must never need a live API.
Models differ in what the API accepts: adaptive thinking exists on 4.6+,
Haiku 4.5 rejects `effort`, some models always think, effort levels differ.

## Decision

1. **`InvestigationModel` Protocol** (`packages/agents/model.py`):
   `decide(DecisionRequest) -> ModelTurn` over provider-neutral transcript
   types; raises `ModelError(code, retryable)`. The engine depends only on
   this. `ClaudeInvestigationModel` (`packages/agents/claude.py`) is the
   only module importing `anthropic` (enforced by a boundary test);
   `FakeInvestigationModel` serves tests.
2. **Configuration** via `InvestigationSettings`: `INVESTIGATION_PROVIDER` (formerly `INVESTIGATION_MODEL_PROVIDER`, still accepted)
   (`anthropic`), `INVESTIGATION_MODEL`, `INVESTIGATION_EFFORT`,
   `INVESTIGATION_THINKING` (`auto|off`), `INVESTIGATION_MAX_TOKENS`,
   timeouts/retries, budgets. `resolve_model_spec` combines settings with
   `MODEL_PROFILES` into a `ModelSpec`; unsupported combinations (e.g. an
   effort level a model rejects) are a startup `ModelConfigError`, and
   unknown model ids run with no thinking/effort parameters rather than
   failing.
3. **Per-investigation pinning.** Provider, model and resolved settings are
   persisted on the investigation row at creation and used on resume, so a
   config change never alters a running investigation and every trace names
   the model that produced it (plus `PROMPT_VERSION`).
4. **Official SDK, manual loop.** `client.messages.create` (non-streaming),
   prompt caching via top-level `cache_control`, adaptive thinking and
   `output_config.effort` only where the profile allows, `tool_choice` left
   `auto`. Assistant content blocks (including thinking signatures) are
   persisted and replayed unchanged. Claude Code is a build tool here, not a
   runtime dependency.

## Alternatives considered

- **Model name as a constant in the engine.** Rejected by requirement.
- **Generic multi-provider abstraction (LiteLLM etc.).** Rejected: hides
  provider features (thinking replay, caching) the loop depends on; a second
  provider can implement the Protocol when needed.

## Consequences

- Benchmarking another model is `INVESTIGATION_MODEL=...`; the e2e test
  proves the configured name reaches the factory and is stamped on the row.
- New models need a `MODEL_PROFILES` entry to use thinking/effort; without
  one they still run, conservatively.

## Amendment (Phase 6)

The default runtime model is now `claude-haiku-4-5` — the initial,
inexpensive runtime model; real investigations and live evaluations run on
it. Changing it remains a configuration change. Comparing models is not a
project requirement and no benchmarking infrastructure exists. The API key
is resolved by `AnthropicCredentials` (environment or `.env`, `SecretStr`),
separately from the persisted `ModelSpec`, so it never reaches a trace.
