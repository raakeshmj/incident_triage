#!/usr/bin/env python3
"""Send a synthetic alert into the running API and print the resulting
incident.

Usage:
    python simulator/send_alert.py
    python simulator/send_alert.py --severity warning --service checkout

This is the fastest way to exercise the full Phase 1 vertical slice by
hand: POST /api/v1/alerts -> GET /api/v1/incidents/{id}.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid

import httpx


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default=os.environ.get("API_URL", "http://localhost:8000"))
    parser.add_argument(
        "--source", default="prometheus", choices=["prometheus", "pagerduty", "generic"]
    )
    parser.add_argument("--service", default="checkout")
    parser.add_argument("--environment", default="production")
    parser.add_argument("--severity", default="critical", choices=["critical", "warning", "info"])
    parser.add_argument(
        "--external-id", default=None, help="omit to let the server derive an idempotency key"
    )
    args = parser.parse_args()

    payload = {
        "source": args.source,
        "external_id": args.external_id,
        "labels": {
            "service": args.service,
            "environment": args.environment,
            "alertname": "HighErrorRate",
        },
        "annotations": {"summary": f"Synthetic alert for {args.service}"},
        "severity": args.severity,
        "status": "firing",
    }

    with httpx.Client(base_url=args.api_url, timeout=10.0) as client:
        request_id = str(uuid.uuid4())
        response = client.post("/api/v1/alerts", json=payload, headers={"X-Request-ID": request_id})
        response.raise_for_status()
        accepted = response.json()
        print("POST /api/v1/alerts ->", json.dumps(accepted, indent=2))

        incident_id = accepted["incident_id"]
        incident_response = client.get(f"/api/v1/incidents/{incident_id}")
        incident_response.raise_for_status()
        print(f"\nGET /api/v1/incidents/{incident_id} ->")
        print(json.dumps(incident_response.json(), indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
