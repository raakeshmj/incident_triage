# packages/evaluation

Phase 6: evaluation and replay for the investigation engine. Design and
grading methodology: `docs/architecture/11-evaluation-architecture.md`
("Phase 6: as built"), ADR-0023.

- `scenario.py` -- golden-scenario schema (world + grading key) and loader
  for `evals/scenarios/*.json`; `leaked_expectations` checks model inputs.
- `world.py` -- `ScenarioWorld`: a scenario's telemetry and change records
  served through the real adapters and `EvidenceService`.
- `heuristic.py` -- `HeuristicInvestigator`, the deterministic model for
  `--mode fake`; reads only tool results.
- `recording.py` -- `InvestigationRecording` (`recording-v1`), export from
  the database, secret scrubbing, save/load.
- `replay.py` -- `render_timeline` (inspect) and `verify_replay`
  (deterministic re-execution with `ReplayModel` + `ReplayToolset`).
- `grading.py` -- `grade(recording, scenario)` and `aggregate(grades)`.
- `harness.py` -- `run_scenario`, `run_batch`, the evaluation-database
  environment.
- `cli.py` -- `evaluate` and `replay` (console scripts; also
  `python -m packages.evaluation`).

Never imports a model SDK; live mode goes through
`packages.agents.factory`. Never calls a telemetry backend.
