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


class FakeMetrics:
    def __init__(self) -> None:
        self.gauges: list[tuple[str, int]] = []
        self.increments: list[str] = []

    def gauge(self, name: str, value: int) -> None:
        self.gauges.append((name, value))

    def increment(self, name: str) -> None:
        self.increments.append(name)


class FixedClock:
    @staticmethod
    def now(tz):
        assert tz is UTC
        return T1


def test_a_fresh_state_has_no_success_and_the_normal_interval():
    state = poller.PollerState()

    assert state.last_success_at is None
    assert state.backoff_seconds() == poller.POLL_INTERVAL_SECONDS


def test_recording_a_poll_sets_the_success_time():
    state = poller.PollerState()
    state.record_poll(T0)

    assert state.last_success_at == T0


def test_failures_before_any_success_count_as_failing():
    state = poller.PollerState()

    assert state.record_failure() == (poller.PollerPhase.FAILING, 1)
    assert state.last_success_at is None


def test_failure_becomes_persistent_exactly_at_the_threshold():
    state = poller.PollerState()
    state.record_poll(T0)

    assert state.record_failure() == (poller.PollerPhase.FAILING, 1)
    assert state.record_failure() == (poller.PollerPhase.FAILING, 2)
    assert state.record_failure() == (poller.PollerPhase.PERSISTENTLY_FAILING, 3)
    assert state.record_failure() == (poller.PollerPhase.PERSISTENTLY_FAILING, 4)
    assert state.last_success_at == T0


def test_recovery_after_failures_resets_the_count():
    state = failed_state(4)
    state.record_poll(T1)
    state.record_recovery()

    assert state.last_success_at == T1
    assert state.backoff_seconds() == poller.POLL_INTERVAL_SECONDS
    assert state.record_failure() == (poller.PollerPhase.FAILING, 1)


@pytest.mark.parametrize(
    ("failures", "expected"),
    [(0, 60), (1, 120), (2, 240), (3, 480), (4, 900), (50, 900)],
)
def test_backoff_doubles_per_failure_up_to_the_cap(failures, expected):
    assert failed_state(failures).backoff_seconds() == expected


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


def test_run_forever_stops_when_the_event_is_set(monkeypatch):
    stop_event = threading.Event()
    stop_event.set()
    monkeypatch.setattr(poller, "_poll_once", lambda: pytest.fail("should not poll when already stopped"))

    poller.run_forever(stop_event, poller.PollerState())


def test_run_forever_records_when_a_cycle_succeeded(tmp_path, monkeypatch):
    import src.core.db as core_db

    monkeypatch.setattr(core_db, "DB_PATH", tmp_path / "findmy.db")
    core_db.init_db()
    state = failed_state(2)
    stop_event = threading.Event()
    monkeypatch.setattr(poller, "_poll_once", stop_event.set)
    monkeypatch.setattr(poller, "datetime", FixedClock)
    fake_metrics = FakeMetrics()
    monkeypatch.setattr(poller, "metrics", fake_metrics)
    waits = []
    monkeypatch.setattr(stop_event, "wait", waits.append)

    poller.run_forever(stop_event, state)

    assert state.last_success_at == T1
    assert waits == [poller.POLL_INTERVAL_SECONDS]
    assert fake_metrics.gauges == [("consecutive_failures", 0)]


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


def test_a_wal_connection_failure_after_a_good_poll_still_counts_as_another_failure(
    client, monkeypatch, caplog
):
    import src.web.findmy.api as findmy_api

    state = failed_state(2)
    stop_event = threading.Event()
    monkeypatch.setattr(poller, "_poll_once", stop_event.set)
    monkeypatch.setattr(poller, "datetime", FixedClock)

    def broken_connection():
        raise OSError("disk gone")

    monkeypatch.setattr(poller, "get_connection", broken_connection)
    fake_metrics = FakeMetrics()
    monkeypatch.setattr(poller, "metrics", fake_metrics)
    waits = []
    monkeypatch.setattr(stop_event, "wait", waits.append)
    monkeypatch.setattr(findmy_api, "poller_state", state)

    with caplog.at_level(logging.WARNING, logger=poller.logger.name):
        poller.run_forever(stop_event, state)

    assert [record.levelno for record in caplog.records] == [logging.ERROR]
    assert "failed 3 times in a row" in caplog.records[0].getMessage()
    assert waits == [480]
    assert fake_metrics.gauges == [("consecutive_failures", 3)]
    assert fake_metrics.increments == ["failure"]
    assert client.get("/api/findmy/status").get_json() == {"last_updated": T1.isoformat()}


def test_the_background_thread_and_the_status_route_share_one_state(monkeypatch):
    import src.web.findmy.api as findmy_api

    started = []

    class RecordingThread:
        def __init__(self, target, args, daemon):
            started.append((target, args, daemon))

        def start(self):
            pass

    monkeypatch.setattr(poller.threading, "Thread", RecordingThread)

    stop_event = poller.start_background_poller()

    assert started == [(poller.run_forever, (stop_event, poller.poller_state), True)]
    assert findmy_api.poller_state is poller.poller_state
