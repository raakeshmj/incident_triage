# packages/remediation

- `catalog.py` -- the action catalog (`catalog-2026.09-1`): the only
  remediation vocabulary; strict parameter models, blast radius, timeouts,
  attempt limits, retry safety, verification requirements.
- `planner.py` -- deterministic RCA -> proposal, parameters taken from the
  evidence the RCA cites.
- `executor.py` -- the `RemediationExecutor` boundary and
  `SimulatorRemediationExecutor` (catalog actions on the simulated
  environment; idempotent, compare-and-set, deadline-bounded, logged).
- `runner.py` -- claims authorized attempts from incident-core, calls the
  executor under the deadline, reconciles lost attempts, records outcomes.

State and every safety decision live in incident-core and the policy
engine; nothing here is reachable from the investigation agent.
