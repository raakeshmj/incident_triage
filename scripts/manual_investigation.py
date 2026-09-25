#!/usr/bin/env python3
"""MANUAL, real-model investigation run. Not part of any test suite -- it
spends real API tokens. Run it by hand:

    make infra-up-full && make migrate
    export ANTHROPIC_API_KEY=...            # or any credential the SDK resolves
    python scripts/manual_investigation.py  # uses INVESTIGATION_MODEL (default claude-sonnet-4-6)

What it does, end to end on the live stack:
 1. starts the deterministic `bad-deployment` chaos scenario on checkout-service;
 2. serves the real API so Alertmanager's webhook lands, and waits for the
    incident that the real HighErrorRate alert opens;
 3. starts an investigation with the configured runtime model and runs the
    real engine -- the model gets only the incident context, alerts, the
    investigation tools and whatever evidence it asks for; it is never told
    the scenario or the expected cause;
 4. prints what it did and exports the complete trace (context, every model
    turn, every tool call and result, hypothesis changes, outcome, RCA) to
    investigation-traces/<investigation_id>.json;
 5. stops the chaos scenario (recording the rollback) no matter what.

Refuses Opus-class models unless --allow-opus (the Phase 5 brief: the
runtime test runs on the configured runtime model, not Opus 5.5).
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import uvicorn
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
load_dotenv(ROOT / ".env")

from apps.worker.investigation_main import build_runtime  # noqa: E402
from packages.agents.config import InvestigationSettings, resolve_model_spec  # noqa: E402
from packages.incident.db.base import make_engine, make_session_factory  # noqa: E402
from packages.incident.db.models import IncidentRow  # noqa: E402
from simulator.changes.registry import ChangeRegistry  # noqa: E402
from simulator.chaos import cli as chaos  # noqa: E402

SERVICE = "checkout-service"


def _credentials_present() -> bool:
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    return (Path.home() / ".config" / "anthropic").exists()


def _serve_api(port: int) -> uvicorn.Server:
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            print(f"API already listening on :{port} -- using it")
            return None  # type: ignore[return-value]
    from apps.api.main import app

    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(50):
        try:
            httpx.get(f"http://localhost:{port}/healthz", timeout=1)
            return server
        except httpx.HTTPError:
            time.sleep(0.2)
    raise SystemExit("API server did not start")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-opus", action="store_true")
    parser.add_argument("--incident-timeout", type=int, default=240)
    args = parser.parse_args()

    settings = InvestigationSettings()
    spec = resolve_model_spec(settings)
    if spec.model.startswith("claude-opus") and not args.allow_opus:
        print(f"refusing to run the manual investigation on {spec.model}; see --help")
        return 2
    if not _credentials_present():
        print("no Anthropic credentials found (ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / profile)")
        return 2
    print(
        f"runtime model: {spec.provider}/{spec.model} thinking={spec.thinking} effort={spec.effort}"
    )

    session_factory = make_session_factory(make_engine())
    server = _serve_api(int(os.environ.get("API_PORT", "8000")))
    import redis

    ChangeRegistry(redis.Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)).seed()

    started_at = datetime.now(UTC)
    chaos.main(["stop", "--service", SERVICE])
    chaos.main(["start", "bad-deployment", "--service", SERVICE, "--duration", "900"])
    try:
        print("waiting for the real alert to open an incident ...")
        incident = None
        deadline = time.monotonic() + args.incident_timeout
        while incident is None and time.monotonic() < deadline:
            with session_factory() as session:
                incident = (
                    session.query(IncidentRow)
                    .filter(IncidentRow.service == SERVICE, IncidentRow.created_at >= started_at)
                    .one_or_none()
                )
            if incident is None:
                time.sleep(3)
        if incident is None:
            print("no incident within the timeout")
            return 1
        print(f"incident {incident.id} opened at {incident.created_at.isoformat()}")

        runtime = build_runtime(settings=settings, owner="manual-investigation")
        investigation_id = runtime.start(incident.id)
        print(f"investigation {investigation_id} started; running the engine ...")
        t0 = time.monotonic()
        outcome = runtime.engine.run(investigation_id)
        elapsed = time.monotonic() - t0

        trace = runtime.investigations.get_trace(investigation_id)
        out_dir = ROOT / "investigation-traces"
        out_dir.mkdir(exist_ok=True)
        path = out_dir / f"{investigation_id}.json"
        path.write_text(json.dumps(trace, indent=2, default=str))

        inv = trace["investigation"]
        with session_factory() as session:
            final_status = session.get(IncidentRow, incident.id).status  # type: ignore[union-attr]
        print(
            f"\noutcome: {outcome.value if outcome else None} in {elapsed:.0f}s; "
            f"incident is {final_status}"
        )
        print(
            f"iterations={inv['iteration_count']} tool_calls={inv['tool_call_count']} "
            f"evidence={inv['evidence_count']} tokens in/out/cache_read="
            f"{inv['input_tokens']}/{inv['output_tokens']}/{inv['cache_read_tokens']}"
        )
        for step in trace["steps"]:
            payload = step["payload"]
            if step["kind"] == "tool_call":
                print(
                    f"  [{step['iteration']}] {payload['tool']}({json.dumps(payload['arguments'])})"
                    f" -> {'ok' if payload['ok'] else payload.get('error_code')}: "
                    f"{(payload.get('summary') or '')[:120]}"
                )
            elif step["kind"] == "hypothesis_update":
                for applied in payload["applied"]:
                    print(
                        f"  [{step['iteration']}] {applied['key']}: "
                        f"{applied['from_status']} -> {applied['to_status']}"
                    )
                for rejected in payload["rejected"]:
                    print(
                        f"  [{step['iteration']}] REJECTED update {rejected['key']}: "
                        f"{rejected['problems']}"
                    )
            elif step["kind"] in ("conclusion_rejected", "invalid_call", "model_error"):
                print(f"  [{step['iteration']}] {step['kind']}: {json.dumps(payload)[:200]}")
        if trace["rca_report"]:
            print("\n" + trace["rca_report"]["summary"])
        else:
            print(f"\nno RCA: {inv['final_result']}")
        print(f"\ntrace exported: {path}")
        return 0
    finally:
        chaos.main(["stop", "--service", SERVICE])
        if server is not None:
            server.should_exit = True


if __name__ == "__main__":
    sys.exit(main())
