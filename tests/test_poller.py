"""Tests for src/findmy/poller.py.

The poller is the piece that runs unattended, so the behaviour worth pinning is
what it does when Apple fetches fail: keep the last known data, back off, and
escalate the log level once the failure looks persistent rather than transient.
"""

import logging
import threading
from datetime import UTC
from datetime import datetime

import pytest

import src.findmy.poller as poller
from src.core.errors import MissingCredentialsError
from tests.conftest import make_item
from tests.conftest import make_location

T0 = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
T1 = datetime(2030, 1, 1, 12, 1, tzinfo=UTC)


def failed_state(failures: int) -> poller.PollerState:
    state = poller.PollerState()
    for _ in range(failures):
        state.record_failure()
    return state


def test_a_fresh_state_has_never_succeeded():
    state = poller.PollerState()

    assert state.phase is poller.PollerPhase.NEVER_SUCCEEDED
    assert state.last_success_at is None
    assert state.consecutive_failures == 0


def test_first_success_makes_the_state_healthy():
    state = poller.PollerState()

    assert state.record_success(T0) is poller.PollerPhase.HEALTHY
    assert state.last_success_at == T0


def test_failures_before_any_success_count_as_failing():
    state = poller.PollerState()

    assert state.record_failure() is poller.PollerPhase.FAILING
    assert state.last_success_at is None


def test_failure_becomes_persistent_exactly_at_the_threshold():
    state = poller.PollerState()
    state.record_success(T0)

    assert state.record_failure() is poller.PollerPhase.FAILING
    assert state.record_failure() is poller.PollerPhase.FAILING
    assert state.record_failure() is poller.PollerPhase.PERSISTENTLY_FAILING
    assert state.consecutive_failures == 3
    assert state.record_failure() is poller.PollerPhase.PERSISTENTLY_FAILING
    assert state.last_success_at == T0


def test_success_after_failures_recovers_and_resets_the_count():
    state = failed_state(4)

    assert state.record_success(T1) is poller.PollerPhase.HEALTHY
    assert state.consecutive_failures == 0
    assert state.last_success_at == T1


@pytest.mark.parametrize(
    ("failures", "expected"),
    [(0, 60), (1, 120), (2, 240), (3, 480), (4, 900), (50, 900)],
)
def test_backoff_doubles_per_failure_up_to_the_cap(failures, expected):
    assert failed_state(failures).backoff_seconds() == expected


def test_backoff_returns_to_the_interval_after_recovery():
    state = failed_state(3)
    state.record_success(T0)

    assert state.backoff_seconds() == poller.POLL_INTERVAL_SECONDS


def test_poll_once_records_fetched_items(tmp_path, monkeypatch):
    import src.core.db as core_db
    import src.findmy.db as db

    monkeypatch.setattr(core_db, "DB_PATH", tmp_path / "findmy.db")
    core_db.init_db()
    monkeypatch.setattr(poller, "fetch_devices", lambda: [make_item("tag-1", make_location(52.5, 13.4))])
    monkeypatch.setattr(poller, "fetch_airtags", lambda: [])

    poller._poll_once()

    with core_db.connection() as conn:
        assert db.latest_location_for(conn, "tag-1")["latitude"] == 52.5


def test_run_forever_survives_a_failing_cycle(monkeypatch):
    """A raising fetch must not kill the loop -- the API keeps serving stale data."""
    stop_event = threading.Event()
    calls = []

    def failing_poll():
        calls.append(1)
        if len(calls) >= 2:
            stop_event.set()
        raise MissingCredentialsError("no credentials")

    monkeypatch.setattr(poller, "_poll_once", failing_poll)
    waits = []
    monkeypatch.setattr(stop_event, "wait", waits.append)
    state = poller.PollerState()

    poller.run_forever(stop_event, state)

    assert len(calls) == 2
    assert waits == [120, 240]
    assert state.phase is poller.PollerPhase.FAILING


def test_run_forever_stops_when_the_event_is_set(monkeypatch):
    stop_event = threading.Event()
    stop_event.set()
    monkeypatch.setattr(poller, "_poll_once", lambda: pytest.fail("should not poll when already stopped"))

    poller.run_forever(stop_event)


def test_run_forever_records_when_a_cycle_succeeded(tmp_path, monkeypatch):
    import src.core.db as core_db

    monkeypatch.setattr(core_db, "DB_PATH", tmp_path / "findmy.db")
    core_db.init_db()
    state = poller.PollerState()
    stop_event = threading.Event()
    monkeypatch.setattr(poller, "_poll_once", stop_event.set)

    poller.run_forever(stop_event, state)

    assert state.phase is poller.PollerPhase.HEALTHY
    assert state.last_success_at is not None


def test_run_forever_logs_an_error_only_from_the_threshold_on(monkeypatch, caplog):
    stop_event = threading.Event()
    calls = []

    def failing_poll():
        calls.append(1)
        if len(calls) >= 4:
            stop_event.set()
        raise MissingCredentialsError("no credentials")

    monkeypatch.setattr(poller, "_poll_once", failing_poll)
    monkeypatch.setattr(stop_event, "wait", lambda _seconds: None)

    with caplog.at_level(logging.WARNING, logger=poller.logger.name):
        poller.run_forever(stop_event, poller.PollerState())

    assert [record.levelno for record in caplog.records] == [
        logging.WARNING,
        logging.WARNING,
        logging.ERROR,
        logging.ERROR,
    ]
    assert "failed 3 times in a row" in caplog.records[2].getMessage()
