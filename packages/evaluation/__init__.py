"""Evaluation and replay for the investigation engine (Phase 6).

scenario.py  golden scenarios (world + grading key)
world.py     a scenario's telemetry served through the real evidence path
heuristic.py deterministic rule-based investigator for `--mode fake`
recording.py the recorded-investigation artifact (+ secret scrubbing)
replay.py    inspect / deterministically re-execute a recording
grading.py   structured grading + aggregation
harness.py   scenario -> investigation -> recording -> grade -> files
cli.py       `evaluate` / `replay`

See docs/architecture/11-evaluation-architecture.md.
"""
