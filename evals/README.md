# evals

The golden dataset for `packages/evaluation` (Phase 6).

- `scenarios/*.json` -- 17 scenarios: 10 where the correct outcome is an
  evidence-backed RCA and 7 hard negatives where it is escalation. Each is a
  deterministic world (metrics, logs, change records, outages, relative to
  the moment the incident opens) plus a grading key. Only the `alerts`
  reach a model directly; everything else is reachable only through tools,
  and the key never is. Schema: `packages/evaluation/scenario.py`.
- `catalog.json` -- the service catalog the scenarios run under.

Add a scenario by adding a file; `evaluate --scenario <id> --mode fake`
shows whether it is solvable from its evidence, and the unit tests validate
it.
