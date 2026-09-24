from __future__ import annotations

from packages.domain.enums import AlertSource
from packages.domain.idempotency import derive_alert_idempotency_key


def test_external_id_present_uses_stable_key_independent_of_payload():
    k1 = derive_alert_idempotency_key(
        source=AlertSource.PAGERDUTY, external_id="abc123", normalized_payload={"x": 1}
    )
    k2 = derive_alert_idempotency_key(
        source=AlertSource.PAGERDUTY, external_id="abc123", normalized_payload={"x": 2}
    )
    assert k1 == k2


def test_external_id_differs_by_source():
    k1 = derive_alert_idempotency_key(
        source=AlertSource.PAGERDUTY, external_id="abc123", normalized_payload={}
    )
    k2 = derive_alert_idempotency_key(
        source=AlertSource.GENERIC, external_id="abc123", normalized_payload={}
    )
    assert k1 != k2


def test_no_external_id_same_payload_same_bucket_same_key():
    now = 1_700_000_000.0
    k1 = derive_alert_idempotency_key(
        source=AlertSource.GENERIC, external_id=None, normalized_payload={"a": 1}, now=now
    )
    k2 = derive_alert_idempotency_key(
        source=AlertSource.GENERIC, external_id=None, normalized_payload={"a": 1}, now=now
    )
    assert k1 == k2


def test_no_external_id_different_payload_different_key():
    now = 1_700_000_000.0
    k1 = derive_alert_idempotency_key(
        source=AlertSource.GENERIC, external_id=None, normalized_payload={"a": 1}, now=now
    )
    k2 = derive_alert_idempotency_key(
        source=AlertSource.GENERIC, external_id=None, normalized_payload={"a": 2}, now=now
    )
    assert k1 != k2


def test_no_external_id_different_debounce_bucket_different_key():
    k1 = derive_alert_idempotency_key(
        source=AlertSource.GENERIC,
        external_id=None,
        normalized_payload={"a": 1},
        now=0,
        debounce_seconds=60,
    )
    k2 = derive_alert_idempotency_key(
        source=AlertSource.GENERIC,
        external_id=None,
        normalized_payload={"a": 1},
        now=1000,
        debounce_seconds=60,
    )
    assert k1 != k2
