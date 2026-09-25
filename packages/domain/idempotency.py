"""Idempotency key derivation for inbound alerts.

Per docs/architecture/06-database-design.md ("Alert deduplication and
retries"): alert-ingestion derives this key, using the source's
`external_id` when present, else a content hash of the normalized payload
plus a debounce time bucket. Pure function so it's testable without
FastAPI or a clock mock beyond passing `now`.
"""

from __future__ import annotations

import hashlib
import json
import time

from packages.domain.enums import AlertSource, AlertStatus

DEFAULT_DEBOUNCE_SECONDS = 60


def derive_alert_idempotency_key(
    *,
    source: AlertSource,
    external_id: str | None,
    normalized_payload: dict,
    status: AlertStatus = AlertStatus.FIRING,
    debounce_seconds: int = DEFAULT_DEBOUNCE_SECONDS,
    now: float | None = None,
) -> str:
    if external_id:
        # The firing and the resolved notification for the same external
        # alert are two different commands and must not share a ledger key,
        # or the resolution would be swallowed as a "duplicate" of the
        # firing notification (the Phase 3 limitation Phase 4 fixes). The
        # firing key keeps its pre-Phase-4 shape so existing ledger rows
        # still dedup redeliveries.
        key = f"{source.value}:external:{external_id}"
        return f"{key}:resolved" if status is AlertStatus.RESOLVED else key

    bucket = int((now if now is not None else time.time()) // debounce_seconds)
    normalized = json.dumps(normalized_payload, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"{source.value}:content:{digest}:{bucket}"
