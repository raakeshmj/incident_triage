from __future__ import annotations

from packages.domain.correlation import compute_correlation_key, compute_fingerprint
from packages.domain.enums import AlertSource


def test_fingerprint_deterministic_regardless_of_label_order():
    a = compute_fingerprint(AlertSource.PROMETHEUS, {"service": "x", "environment": "prod"})
    b = compute_fingerprint(AlertSource.PROMETHEUS, {"environment": "prod", "service": "x"})
    assert a == b


def test_fingerprint_differs_by_source():
    labels = {"service": "x", "environment": "prod"}
    a = compute_fingerprint(AlertSource.PROMETHEUS, labels)
    b = compute_fingerprint(AlertSource.PAGERDUTY, labels)
    assert a != b


def test_fingerprint_differs_by_labels():
    a = compute_fingerprint(AlertSource.PROMETHEUS, {"service": "x", "environment": "prod"})
    b = compute_fingerprint(AlertSource.PROMETHEUS, {"service": "y", "environment": "prod"})
    assert a != b


def test_fingerprint_is_a_stable_hex_digest():
    fp = compute_fingerprint(AlertSource.GENERIC, {"service": "x", "environment": "prod"})
    assert len(fp) == 64
    int(fp, 16)  # raises ValueError if not hex


def test_correlation_key_is_fingerprint_in_phase_1():
    fp = compute_fingerprint(AlertSource.GENERIC, {"service": "x", "environment": "prod"})
    assert compute_correlation_key(fp) == fp
