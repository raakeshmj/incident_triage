#!/usr/bin/env python3
"""Operator CLI for the 7 chaos scenarios (simulator/chaos/scenarios.py).

Run from the host (not inside a container) against the docker-composed
Redis, same as `simulator/send_alert.py` runs against the host-exposed API.

    python -m simulator.chaos.cli start high-cpu --service checkout-service
    python -m simulator.chaos.cli start dependency-failure --service inventory-service \
        --duration 120
    python -m simulator.chaos.cli status
    python -m simulator.chaos.cli stop --service checkout-service
    python -m simulator.chaos.cli list

State lives at Redis key `chaos:{service}` as
`{"scenario": ..., "params": ..., "started_at": ..., "expires_at": ...}`,
read by `simulator.services.common.chaos.ChaosController` inside each
service (self-expiring, so a forgotten scenario doesn't run forever).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import redis
from simulator.chaos.scenarios import SCENARIOS, get


def _redis_client() -> redis.Redis:
    url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    return redis.Redis.from_url(url, decode_responses=True)


def cmd_start(args: argparse.Namespace) -> int:
    scenario = get(args.scenario)
    overrides = json.loads(args.params) if args.params else None
    params = scenario.params(overrides)
    duration = args.duration or scenario.default_duration_seconds
    now = time.time()
    state = {
        "scenario": scenario.name,
        "params": params,
        "started_at": now,
        "expires_at": now + duration,
    }
    client = _redis_client()
    client.set(f"chaos:{args.service}", json.dumps(state))
    print(f"started {scenario.name!r} on {args.service} for {duration}s: {params}")
    print(f"expected effect: {scenario.effect.replace('<svc>', args.service)}")
    print(f"watch for alert: {scenario.fires_alert}")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    client = _redis_client()
    deleted = client.delete(f"chaos:{args.service}")
    print(f"stopped chaos on {args.service}" if deleted else f"no active chaos on {args.service}")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    client = _redis_client()
    found = False
    for key in client.scan_iter("chaos:*"):
        raw = client.get(key)
        if not raw:
            continue
        state = json.loads(raw)
        remaining = max(0, int(state["expires_at"] - time.time()))
        service_name = key.removeprefix("chaos:")
        print(f"{service_name}: {state['scenario']} ({remaining}s remaining) {state['params']}")
        found = True
    if not found:
        print("no active chaos scenarios")
    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    for scenario in SCENARIOS.values():
        print(f"{scenario.name}")
        print(f"  trigger: {scenario.trigger}")
        print(f"  effect:  {scenario.effect}")
        print(f"  alert:   {scenario.fires_alert}")
        print(f"  default duration: {scenario.default_duration_seconds}s")
        print(f"  default params:   {scenario.default_params}")
    return 0


_SERVICES = ["checkout-service", "payment-service", "inventory-service"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="start a chaos scenario on one service")
    start.add_argument("scenario", choices=sorted(SCENARIOS))
    start.add_argument("--service", required=True, choices=_SERVICES)
    start.add_argument(
        "--duration", type=int, default=None, help="seconds; defaults to the scenario's default"
    )
    start.add_argument("--params", default=None, help="JSON object overriding default params")
    start.set_defaults(func=cmd_start)

    stop = subparsers.add_parser("stop", help="stop chaos on one service")
    stop.add_argument("--service", required=True, choices=_SERVICES)
    stop.set_defaults(func=cmd_stop)

    status = subparsers.add_parser("status", help="show all active chaos scenarios")
    status.set_defaults(func=cmd_status)

    listing = subparsers.add_parser("list", help="describe all 7 scenarios")
    listing.set_defaults(func=cmd_list)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
