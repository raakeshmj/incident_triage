"""Real Postgres: concurrent alert processing for the same
(service, environment) must not create duplicate incidents -- Case E in
the Phase 2 correlation behavior notes, and
docs/adr/0015-deterministic-correlation-scoring-engine.md's advisory-lock
design.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select

from packages.domain.enums import AlertSource
from packages.incident.db.models import AlertRow, IncidentRow
from tests.factories import make_alert_command


def test_concurrent_related_alerts_create_exactly_one_incident(core, session_factory):
    """N threads, each posting a *distinct* alert (different external_id,
    so none of them are command-idempotency duplicates of each other) for
    the same service/environment/region -- strong enough correlation
    signals that, processed sequentially, they'd obviously all land on one
    incident. Run concurrently, they still must.
    """
    n = 8

    def send(i: int):
        return core.handle_alert_received(
            make_alert_command(
                source=AlertSource.PROMETHEUS,
                external_id=f"race-{i}",
                service="racy-checkout",
                environment="production",
                extra_labels={"region": "us-east"},
            )
        )

    with ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(send, range(n)))

    with session_factory() as session:
        incidents = (
            session.execute(select(IncidentRow).where(IncidentRow.service == "racy-checkout"))
            .scalars()
            .all()
        )
        alerts = (
            session.execute(select(AlertRow).where(AlertRow.external_id.like("race-%")))
            .scalars()
            .all()
        )

    assert len(incidents) == 1, f"expected exactly one incident, got {len(incidents)}"
    assert len(alerts) == n
    assert {a.incident_id for a in alerts} == {incidents[0].id}
    assert {r.incident_id for r in results} == {incidents[0].id}
    assert sum(1 for r in results if r.incident_created) == 1


def test_concurrent_unrelated_alerts_create_separate_incidents(core, session_factory):
    """Sanity check on the other side of the same mechanism: concurrency
    control must not over-merge unrelated alerts just because they raced.
    """
    n = 6

    def send(i: int):
        return core.handle_alert_received(
            make_alert_command(
                external_id=f"unrelated-{i}",
                service=f"service-{i}",  # a different service each time
                environment="production",
            )
        )

    with ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(send, range(n)))

    assert len({r.incident_id for r in results}) == n
    assert all(r.incident_created for r in results)
