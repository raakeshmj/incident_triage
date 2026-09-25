#!/usr/bin/env python3
"""Seed / inspect the simulated change registries.

python -m simulator.changes.cli seed     # idempotent; `make infra-up-full` runs it
python -m simulator.changes.cli list
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import redis

from simulator.changes.registry import CONFIG_KEY, DEPLOYMENTS_KEY, SEED_SERVICES, ChangeRegistry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["seed", "list"])
    args = parser.parse_args(argv)
    client = redis.Redis.from_url(
        os.environ.get("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True
    )
    registry = ChangeRegistry(client)
    if args.command == "seed":
        seeded = registry.seed()
        print(f"seeded: {', '.join(seeded)}" if seeded else "already seeded")
        return 0
    for service in SEED_SERVICES:
        for label, key in (("deployments", DEPLOYMENTS_KEY), ("config", CONFIG_KEY)):
            for raw in client.lrange(key.format(service=service), 0, -1):
                print(f"{service} {label}: {json.dumps(json.loads(raw))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
