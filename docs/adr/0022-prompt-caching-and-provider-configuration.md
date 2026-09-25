# ADR-0022: Prompt caching of the stable prefix; provider-neutral configuration

Status: Accepted

## Context

Every iteration of an investigation re-sends the same instructions and tool
definitions (~5k tokens) followed by a growing, investigation-specific
transcript. Phase 5 used the Anthropic API's top-level (automatic)
`cache_control`, which places the breakpoint on the last block -- the newest
tool results -- so the cached material was mostly volatile incident data,
and the system prompt embedded the per-investigation budget, making the
"stable" part differ between investigations. Separately, no credential is
available yet, and the runtime provider must not be a hard dependency of
startup or tests.

## Decision

1. **Stable vs dynamic, by construction.** The system prompt contains only
   policy (derived from the stopping criteria); everything per-incident or
   per-investigation, including the budget, is in the transcript. Tool
   definitions are static and deterministically ordered. The stable prefix
   is fingerprinted (`stable_prefix_digest`) and recorded.
2. **Cache only the stable prefix, only in the adapter.** The Anthropic
   adapter puts one explicit ephemeral breakpoint on the system block
   (covering tools + system) and none in `messages`. Incident context, tool
   results and notices are never marked. The engine stays provider-agnostic
   and records the adapter's cache metadata and the provider's cache token
   counts.
3. **Capability-gated.** `PROVIDER_PROFILES` says whether a provider/model
   supports caching and its minimum cacheable length;
   `INVESTIGATION_PROMPT_CACHE=auto|off`. Unsupported → no hints, same
   behaviour otherwise.
4. **Provider-neutral configuration.** `INVESTIGATION_PROVIDER` /
   `INVESTIGATION_MODEL` are placeholders. A `PROVIDERS` registry builds the
   model and resolves that provider's credentials lazily; nothing else
   needs them. A build failure ends that investigation `FAILED /
   model_config_error`.

## Alternatives considered

- **Keep automatic caching (or add a conversation breakpoint).** Within one
  investigation the transcript is append-only, so caching it would cut
  input cost further. Rejected for now by requirement: tool results and
  incident state are volatile/sensitive and are not to be cached. The
  explicit system breakpoint is the one to keep regardless; a conversation
  breakpoint can be added later behind the same profile flag.
- **An application-level response cache.** Rejected: not what prompt
  caching is, and it would replay stale answers.

## Consequences

- Cache hits span investigations (same prefix, 5-minute TTL), not just
  iterations -- but only if the prefix meets the model's minimum (Haiku 4.5:
  4,096 tokens; ours is estimated at ~5k). Only a live run's
  `cache_read_input_tokens` confirms it.
- Changing the system prompt or tools invalidates the cache and must bump
  `PROMPT_VERSION`; the digest makes an accidental change visible.
