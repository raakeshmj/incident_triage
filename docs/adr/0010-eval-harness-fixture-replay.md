# ADR-0010: Evaluation is an offline, fixture-replay pipeline gating CI, not an online experiment

Status: Accepted

## Context

We need to know whether changes to the agent's prompt, tools, or model
version make root-cause analysis better or worse before they reach
production incidents, and we need this to be cheap and fast enough to run
routinely (ideally on every relevant change), not just occasionally by
hand.

## Decision

`eval-harness` runs the investigation agent against a versioned golden
dataset of fixture bundles (frozen evidence + human-labeled ground truth),
using `evidence-service`'s `replay` mode so evidence retrieval is
deterministic and free of live-system dependency. It scores root-cause
precision/recall, evidence groundedness, remediation correctness, and
runs a separate, model-independent policy-safety suite. Regressions beyond
threshold block CI. See `architecture/11-evaluation-architecture.md`.

## Alternatives considered

- **Online A/B testing against live incidents**: rejected for this stage —
  too risky to experiment with prompt/tool changes on real production
  incidents before there's an offline signal that the change isn't a
  regression; may be layered on top later as a secondary signal, not a
  replacement.
- **Manual spot-checking only, no automated harness**: rejected — doesn't
  scale, isn't repeatable, and gives no CI gate, so regressions would only
  be caught after they've already shipped.

## Consequences

Prompt/tool/model changes get a fast, repeatable, cost-bounded signal
before merge. The dataset itself becomes a first-class asset requiring
ongoing curation (new fixtures added from real incidents, reviewed by a
human before being trusted as ground truth) — an ongoing cost accepted in
exchange for a CI gate that actually means something.
