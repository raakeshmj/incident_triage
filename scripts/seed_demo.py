#!/usr/bin/env python3
"""Seed the local (dev) database with incidents in every lifecycle state,
for the operations console and its browser tests.

Runs golden scenarios through the real lifecycle -- investigation
(heuristic investigator), planner, policy, approval (a simulated human),
execution against the scenario world, verification from evidence -- into
the database the API reads (INCIDENT_CORE_DATABASE_URL). Nothing touches
the simulator or any live system. It *adds* incidents; it never resets.

    python scripts/seed_demo.py --yes

Resulting states: RESOLVED (x4), VERIFICATION_FAILED, ESCALATED (inconclusive
investigation; policy-denied remediation), RCA_READY (no catalog action),
AWAITING_APPROVAL, TRIAGING.
"""

from __future__ import annotations

import argparse
import logging
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from packages.evaluation.harness import (  # noqa: E402
    FAKE_SPEC,
    EvalEnvironment,
    fake_model_factory,
    open_incident,
    run_scenario,
)
from packages.evaluation.scenario import load_scenarios  # noqa: E402
from packages.evidence.db.base import make_engine as make_evidence_engine  # noqa: E402
from packages.evidence.db.base import make_session_factory as make_evidence_sessions  # noqa: E402
from packages.incident.db.base import make_engine, make_session_factory  # noqa: E402
from packages.incident.service import IncidentCoreService  # noqa: E402

# (scenario, environment, lifecycle overrides). Environments keep correlation
# keys apart so each run is its own incident.
PLAN: list[tuple[str, str, dict]] = [
    ("bad-deployment", "production", {}),
    ("memory-pressure", "production", {}),
    ("bad-configuration", "production", {}),
    ("cascading-failure", "staging", {}),
    ("insufficient-evidence", "staging-eu", {}),
    # production-eu is no catalog action's environment: policy denies -> ESCALATED
    ("ineffective-rollback", "production-eu", {"expected_final_state": "ESCALATED"}),
    # bad-deployment's production incident is RESOLVED (closed), so this is new
    ("ineffective-rollback", "production", {}),
    ("service-unavailable", "production", {}),
    (
        "bad-configuration",
        "staging",
        {
            "approve": False,
            "expected_verification": None,
            "expected_final_state": "AWAITING_APPROVAL",
        },
    ),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="write demo incidents to the dev DB")
    args = parser.parse_args()
    if not args.yes:
        print("adds demo incidents to INCIDENT_CORE_DATABASE_URL; pass --yes")
        return 2
    logging.disable(logging.WARNING)
    sessions = make_session_factory(make_engine())
    env = EvalEnvironment(
        incident_sessions=sessions,
        evidence_sessions=make_evidence_sessions(make_evidence_engine()),
        reset=lambda: None,  # add, never wipe
    )
    scenarios = load_scenarios()
    for scenario_id, environment, overrides in PLAN:
        base = scenarios[scenario_id]
        lifecycle = base.lifecycle.model_copy(update=overrides) if base.lifecycle else None
        scenario = base.model_copy(update={"environment": environment, "lifecycle": lifecycle})
        result = run_scenario(
            scenario,
            env,
            mode="fake",
            spec=FAKE_SPEC,
            model_factory=fake_model_factory(),
            run_id=f"demo-{scenario_id}-{uuid.uuid4().hex[:6]}",
            traces_dir=None,
            results_dir=None,
            verification_time_scale=0.004,
        )
        print(
            f"{scenario_id:24} {environment:14} -> {result.recording.incident['final_status']:20} "
            f"incident {result.recording.incident['id']}"
            + (f"  errors: {result.errors}" if result.errors else "")
        )
    triaging = open_incident(
        IncidentCoreService(sessions),
        scenarios["bad-configuration"].model_copy(update={"environment": "staging-us"}),
    )
    print(f"{'(alert only)':24} {'staging-us':14} -> {'TRIAGING':20} incident {triaging}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
