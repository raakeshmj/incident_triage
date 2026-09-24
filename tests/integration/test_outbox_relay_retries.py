"""Real Postgres: the outbox relay's retry/backoff and failure-recording
behavior (Phase 2 requirement 1), using the fault-injection publishers
from tests/fakes.py instead of a real Redis so failure timing is exact.
"""

from __future__ import annotations

from sqlalchemy import select

from apps.worker.main import relay_once
from packages.events.publisher import LoggingEventPublisher
from packages.incident.db.models import OutboxEventRow
from tests.factories import make_alert_command
from tests.fakes import AlwaysFailingPublisher, FlakyPublisher


def test_relay_retries_and_eventually_publishes_on_transient_failure(core, session_factory):
    core.handle_alert_received(make_alert_command())
    flaky = FlakyPublisher(LoggingEventPublisher(), fail_times=2)

    published = relay_once(
        session_factory,
        flaky,
        max_publish_attempts=3,
        retry_backoff_seconds=0.01,
    )

    assert published == 2  # both events (AlertReceived, IncidentCreated) eventually succeed
    assert flaky.attempts == 2 + 2  # 2 failures + 1 success, per event

    with session_factory() as session:
        rows = session.execute(select(OutboxEventRow)).scalars().all()
    assert all(row.published_at is not None for row in rows)
    assert all(row.publish_attempts >= 1 for row in rows)
    assert all(row.last_publish_error is None for row in rows)  # cleared on eventual success


def test_relay_gives_up_this_pass_after_max_attempts_and_records_failure(core, session_factory):
    core.handle_alert_received(make_alert_command())
    always_fails = AlwaysFailingPublisher(error=RuntimeError("redis is down"))

    published = relay_once(
        session_factory,
        always_fails,
        max_publish_attempts=3,
        retry_backoff_seconds=0.01,
    )

    assert published == 0
    assert always_fails.attempts == 2 * 3  # 3 raw publish() attempts per event, 2 events

    with session_factory() as session:
        rows = session.execute(select(OutboxEventRow)).scalars().all()
    assert all(row.published_at is None for row in rows)  # never marked published
    # `publish_attempts` counts relay *passes* that attempted this event, not
    # raw publish() calls (those are only visible via the outbox.publish_retry
    # metric/log) -- one relay_once() call is one pass, regardless of how many
    # times _publish_with_retry looped internally.
    assert all(row.publish_attempts == 1 for row in rows)
    assert all(row.last_publish_error == "redis is down" for row in rows)


def test_unpublished_event_is_retried_on_the_next_relay_pass(core, session_factory):
    """Simulates crash scenario 1 from apps/worker/main.py's docstring:
    the event is untouched after a failed pass and is picked up again.
    """
    core.handle_alert_received(make_alert_command())
    always_fails = AlwaysFailingPublisher()
    assert relay_once(session_factory, always_fails, max_publish_attempts=1) == 0

    # Next pass, with a working publisher -- the same events are still
    # there (nothing was lost) and now succeed.
    flaky = FlakyPublisher(LoggingEventPublisher(), fail_times=0)
    published = relay_once(session_factory, flaky, max_publish_attempts=1)

    assert published == 2
    with session_factory() as session:
        rows = session.execute(select(OutboxEventRow)).scalars().all()
    assert all(row.published_at is not None for row in rows)
    assert all(row.publish_attempts == 2 for row in rows)  # 1 failed + 1 succeeded
