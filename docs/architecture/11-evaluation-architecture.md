# 11 — Evaluation Architecture

## Why this has to be a separate offline pipeline

The runtime path cannot be the place where we measure whether the agent is
any good — it has no ground truth, no labeled root causes, and running
experiments against live incidents would be irresponsible. Evaluation is
therefore an **offline, replayable, CI-gated pipeline** (`eval-harness`),
not a runtime service.

## Golden dataset

- A curated set of historical incidents (real, anonymized, or
  synthetically constructed), each stored as a **fixture bundle**:
  - The original alerts.
  - The full set of evidence records `evidence-service` produced or would
    produce for that incident's time window (recorded once from real
    systems, then frozen).
  - A human-labeled ground truth: the actual root cause, the correct
    (or acceptable) remediation action, and known-wrong hypotheses worth
    penalizing if selected.
- Stored outside the production database (flat files / a dedicated eval
  store), versioned in the repo or an artifact store, so the dataset itself
  has history and review.

## Replay mode

`evidence-service` supports a `replay` backend in addition to `live`: when
running in replay mode, tool calls are served from a fixture bundle instead
of querying Prometheus/Loki/etc. This makes runs **deterministic and free
of live-system/Claude-spend variance** for anything that doesn't need to
hit the real model, and reproducible for anything that does (same fixture
in, same evidence out, only the model's reasoning varies run to run).

```
eval-harness ──▶ investigation-agent (real Claude calls) ──▶ evidence-service (replay backend, serves fixture)
                          │
                          ▼
                 InvestigationResult ──▶ scorer ──▶ metrics
```

## Metrics

| Metric | What it checks | How |
|---|---|---|
| Root-cause precision/recall | Did the selected hypothesis match the labeled ground truth (exact or judged-equivalent)? | Rule-based match where possible; LLM-as-judge (a *separate* Claude call, not the agent under test) with a fixed rubric where semantic equivalence is needed, calibrated against a human-labeled subset |
| Evidence groundedness | Does every cited evidence ID's actual content support the claim it's attached to? | Automated: citation existence is already enforced structurally (§08); groundedness of *meaning* uses LLM-as-judge + periodic human spot-check calibration |
| Refutation search quality | Did the agent look for disconfirming evidence, not just confirming? | Fraction of investigations with ≥1 `refutes` link recorded on the top hypothesis, tracked as a trend, not a hard gate initially |
| Remediation correctness | Does the proposed action_catalog_id + parameters match the labeled acceptable remediation set? | Rule-based |
| Policy safety (adversarial) | Do known-bad proposals get denied? | A fixed suite of proposals that *must* evaluate to `DENY` under the current policy version — pure unit tests on `policy-engine`, no model involved |
| Budget adherence | Tool calls / tokens / wall-clock within configured limits | Direct measurement |
| Cost per investigation | Token spend | Direct measurement |
| Verification-informed outcome (online signal) | For incidents that *did* run in production, did the executed action's verification pass? | Fed back from production `verifications` table into the next dataset curation cycle — this is how the golden set grows over time |

## CI gating

- The policy-safety suite and schema/validation tests run on **every**
  change to `investigation-agent`, `policy-engine`, or the `action_catalog`
  — zero tolerance, blocks merge.
- Root-cause precision/recall and groundedness run on the full golden
  dataset on every change to the agent's system prompt, tool set, or model
  version; a regression beyond a configured threshold blocks merge and
  requires explicit sign-off to override.
- The LLM-as-judge itself is periodically recalibrated: a held-out sample
  of its scores is checked against fresh human labels; drift beyond
  threshold pauses reliance on it for gating until recalibrated.

## What's explicitly out of scope for v1

- Online A/B testing of prompts against live production incidents — too
  risky before the offline suite is trustworthy.
- Automated dataset growth without human review of new labels — a human
  reviews every fixture added to the golden set before it can gate CI.
