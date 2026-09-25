# packages/agents

The Phase 5 investigation agent: a bounded, resumable, hypothesis-driven
loop in which a model gathers evidence through read-only tools and proposes
hypotheses and a conclusion that incident-core validates. Design:
`docs/architecture/15-investigation-engine.md`, ADR-0020, ADR-0021.

- `engine.py` -- `InvestigationEngine`: claim, per-iteration reload, limits,
  model call with retries, action processing, escalation. Holds no state
  between iterations; every step goes to incident-core first.
- `model.py` -- the provider-neutral `InvestigationModel` Protocol and
  transcript types. The engine depends only on this.
- `claude.py` -- `ClaudeInvestigationModel`, the only module that imports
  the `anthropic` SDK (boundary-tested).
- `config.py` -- `INVESTIGATION_*` settings, `MODEL_PROFILES`, `ModelSpec`.
  Switching model = changing `INVESTIGATION_MODEL`.
- `factory.py` -- provider name -> model implementation.
- `fake.py` -- `FakeInvestigationModel` for tests (no API calls).
- `prompts.py` -- the versioned system prompt (`PROMPT_VERSION`).
- `context.py` -- the initial incident context (no incident id; sanitized).
- `toolset.py` -- model-facing tool names over `packages/tools`, with
  `incident_id`/`investigation_id` bound outside the model; compact results.

What this package must never do (enforced by `tests/unit/test_boundaries.py`):
import a DB driver, Redis, an HTTP client or `subprocess`; mutate
production; accept an incident id from the model.

Phase 6: `factory.PROVIDERS` is the provider registry (credentials resolved
lazily; `INVESTIGATION_PROVIDER` / `INVESTIGATION_MODEL` are placeholders),
`config.PROVIDER_PROFILES` carries per-model capabilities including prompt
caching, and `claude.py` caches only the stable prefix (system prompt +
tool definitions) -- ADR-0022. `model.DecisionRequest` documents which of
its fields are stable and which dynamic.
