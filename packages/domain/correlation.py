"""Deterministic alert correlation (ADR-0004).

Correlation is plain code, never an LLM call -- it decides which
aggregate (Incident) an alert belongs to, and that decision must be fast,
reproducible, and testable in isolation.

Phase 1 scope: the minimal deterministic rule needed for the "one alert ->
one incident, same alert firing again -> same incident" vertical slice.
`correlation_key` is currently just the alert's `fingerprint`. Grouping
genuinely different alerts into one incident by service/topology/time
window is real, separate engineering work that ADR-0004 already
anticipates as a future iteration -- it is intentionally not implemented
here, and is not required for this phase's vertical slice.
"""

from __future__ import annotations

import hashlib
import json

from packages.domain.enums import AlertSource


def compute_fingerprint(source: AlertSource, labels: dict[str, str]) -> str:
    """Stable hash of (source, labels), independent of key order.

    Used both as `alerts.fingerprint` and, for now, directly as the
    incident's `correlation_key` -- see module docstring.
    """
    normalized = json.dumps(
        {"source": source.value, "labels": labels},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def compute_correlation_key(fingerprint: str) -> str:
    """Phase-1 correlation rule: same fingerprint -> same incident."""
    return fingerprint
