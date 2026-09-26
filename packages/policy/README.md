# packages/policy

`engine.py`: `evaluate(proposal, action_catalog_entry, policy, context) ->
PolicyDecision` -- pure (no I/O; boundary-tested), deterministic, every rule
reported. `DEFAULT_POLICY` (`policy-2026.09-1`) never allows automatic
execution. The context is built and stored by incident-core
(`packages/incident/remediations.py`). See
`docs/architecture/09-remediation-policy-boundaries.md` and ADR-0007/0012/0024.
