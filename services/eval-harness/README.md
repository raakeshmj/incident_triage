# eval-harness

Status: not implemented — design only. See
`docs/architecture/11-evaluation-architecture.md`,
`docs/adr/0010-eval-harness-fixture-replay.md`.

## Responsibility

Offline, CI-gated pipeline (pytest-based). Replays a versioned golden
dataset of fixture bundles through `investigation-agent`, using
`evidence-service`'s `replay` mode for deterministic evidence retrieval.
Scores root-cause precision/recall, evidence groundedness, remediation
correctness; runs a model-independent adversarial policy-safety suite
against `policy-engine` directly.

## Owns

The golden dataset (fixture bundles + human-labeled ground truth), stored
separately from the production database.

## Talks to

- `investigation-agent` (real Claude calls, in replay-evidence mode),
  `evidence-service` (replay mode), `policy-engine` (direct, for the
  policy-safety suite). Not part of the runtime request path — runs in CI
  and on demand.
