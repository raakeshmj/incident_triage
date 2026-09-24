"""Continuous baseline traffic against checkout-service.

Prometheus's alert rules are all `rate(...)`-based (see
`infrastructure/prometheus/alerts/`), which need a steady request stream to
mean anything -- a real production checkout service has continuous
customer traffic; this stands in for that so the environment produces
alertable signal on its own, without a human hand-curling requests.

Deliberately not instrumented with OpenTelemetry itself: it's the traffic
source, not a system under observation.
"""

from __future__ import annotations

import asyncio
import os
import random
import sys
import time
import uuid

import httpx

CHECKOUT_URL = os.environ.get("CHECKOUT_URL", "http://checkout-service:8001")
REQUESTS_PER_SECOND = float(os.environ.get("REQUESTS_PER_SECOND", "5"))
SKUS = ["WIDGET-1", "WIDGET-2", "GADGET-1", "GADGET-2"]


def _order_payload() -> dict:
    return {
        "items": [{"sku": random.choice(SKUS), "quantity": random.randint(1, 3)}],
        "amount": round(random.uniform(5.0, 250.0), 2),
        "currency": "USD",
    }


async def _send_one(client: httpx.AsyncClient) -> None:
    request_id = str(uuid.uuid4())
    try:
        response = await client.post(
            "/checkout", json=_order_payload(), headers={"x-request-id": request_id}, timeout=10.0
        )
        print(f"checkout -> {response.status_code}", file=sys.stderr)
    except httpx.HTTPError as exc:
        print(f"checkout -> error: {exc}", file=sys.stderr)


async def main() -> None:
    interval = 1.0 / REQUESTS_PER_SECOND
    async with httpx.AsyncClient(base_url=CHECKOUT_URL) as client:
        while True:
            start = time.monotonic()
            asyncio.create_task(_send_one(client))
            elapsed = time.monotonic() - start
            await asyncio.sleep(max(0.0, interval - elapsed))


if __name__ == "__main__":
    asyncio.run(main())
