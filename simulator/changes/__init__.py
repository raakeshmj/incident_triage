"""The simulated environment's change systems: a deployment registry (the
"CI/CD system") and a config-change registry (the "config service").

Written by the simulated world itself -- seeded when the stack comes up,
appended to when a chaos scenario performs a deploy/rollback or config push
-- and only *read* by evidence-service's change adapter
(packages/evidence/adapters/changes.py). Nothing here is fabricated after the
fact: a record exists only because the simulated system did that thing.
"""
