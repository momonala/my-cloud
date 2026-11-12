"""Characterization tests for the enter/exit edge trigger in src/findmy/alerts.py.

Times are hardcoded. Telegram is replaced by a fake send_telegram_message that
records the formatted text, and the two alert writes in src/findmy/db.py and the
metrics counter are wrapped so the order of writes, metrics and sends can be
asserted together.
"""

from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest

import src.findmy.alerts as alerts
import src.findmy.db as db
import src.findmy.telegram as telegram
from src.core.env import HOME_LATITUDE
from src.core.env import HOME_LONGITUDE
from src.findmy.alerts import AlertState
from src.findmy.alerts import Transition
from src.findmy.alerts import check_alerts
from src.findmy.alerts import next_state
from src.findmy.db import alerts_for_device
from src.findmy.db import create_alert
from src.findmy.db import record_fetch
from tests.conftest import make_item
from tests.conftest import make_location
from tests.conftest import minutes_later

T0 = datetime(2026, 8, 12, 13, 0, tzinfo=UTC)
PAST = T0 - timedelta(minutes=1)
FUTURE = T0 + timedelta(minutes=4)
NEXT_COOLDOWN = T0 + timedelta(seconds=300)

FAR = (HOME_LATITUDE + 1, HOME_LONGITUDE)
HOME = (HOME_LATITUDE, HOME_LONGITUDE)
NEAR_HOME = (HOME_LATITUDE + 0.0001, HOME_LONGITUDE)
ENTER_TEXT = "📍 *Item tag-1* entered the 100m radius around home"
EXIT_TEXT = "📍 *Item tag-1* left the 100m radius around home"


@pytest.mark.parametrize(
    ("alert_type", "was_inside", "inside", "cooldown_until", "expected"),
    [
        ("enter", False, False, PAST, Transition(AlertState(False, PAST), notify=False)),
        ("enter", False, False, FUTURE, Transition(AlertState(False, FUTURE), notify=False)),
        ("enter", True, True, PAST, Transition(AlertState(True, PAST), notify=False)),
        ("enter", True, True, FUTURE, Transition(AlertState(True, FUTURE), notify=False)),
        ("enter", True, False, PAST, Transition(AlertState(False, PAST), notify=False)),
        ("enter", True, False, FUTURE, Transition(AlertState(False, FUTURE), notify=False)),
        ("enter", False, True, PAST, Transition(AlertState(True, NEXT_COOLDOWN), notify=True)),
        ("enter", False, True, FUTURE, Transition(AlertState(False, FUTURE), notify=False)),
        ("exit", False, False, PAST, Transition(AlertState(False, PAST), notify=False)),
        ("exit", False, False, FUTURE, Transition(AlertState(False, FUTURE), notify=False)),
        ("exit", True, True, PAST, Transition(AlertState(True, PAST), notify=False)),
        ("exit", True, True, FUTURE, Transition(AlertState(True, FUTURE), notify=False)),
        ("exit", False, True, PAST, Transition(AlertState(True, PAST), notify=False)),
        ("exit", False, True, FUTURE, Transition(AlertState(True, FUTURE), notify=False)),
        ("exit", True, False, PAST, Transition(AlertState(False, NEXT_COOLDOWN), notify=True)),
        ("exit", True, False, FUTURE, Transition(AlertState(True, FUTURE), notify=False)),
    ],
)
def test_next_state_for_every_state_and_observation(alert_type, was_inside, inside, cooldown_until, expected):
    state = AlertState(inside=was_inside, cooldown_until=cooldown_until)

    assert next_state(state, inside, T0, alert_type=alert_type) == expected


@pytest.mark.parametrize(
    ("alert_type", "was_inside", "inside"), [("enter", False, True), ("exit", True, False)]
)
def test_next_state_notifies_exactly_at_the_cooldown_boundary(alert_type, was_inside, inside):
    at_boundary = AlertState(inside=was_inside, cooldown_until=T0)
    just_before = AlertState(inside=was_inside, cooldown_until=T0 + timedelta(microseconds=1))

    assert next_state(at_boundary, inside, T0, alert_type=alert_type).notify is True
    assert next_state(just_before, inside, T0, alert_type=alert_type) == Transition(just_before, notify=False)


def test_alert_state_cooldown_ends_exactly_300s_after_the_last_trigger():
    at_boundary = AlertState.from_row(
        {"is_active": 0, "triggered_at": (T0 - timedelta(seconds=300)).isoformat()}
    )
    just_before = AlertState.from_row(
        {
            "is_active": 0,
            "triggered_at": (T0 - timedelta(seconds=300) + timedelta(microseconds=1)).isoformat(),
        }
    )

    assert at_boundary == AlertState(inside=False, cooldown_until=T0)
    assert at_boundary.cooldown_elapsed(T0) is True
    assert just_before.cooldown_elapsed(T0) is False


def test_alert_state_with_no_prior_trigger_has_no_cooldown():
    state = AlertState.from_row({"is_active": 0, "triggered_at": None})

    assert state == AlertState(inside=False, cooldown_until=None)
    assert next_state(state, True, T0, alert_type="enter") == Transition(
        AlertState(True, NEXT_COOLDOWN), notify=True
    )


@pytest.fixture
def calls(monkeypatch):
    """Every alert write, metric and Telegram send, in the order they happened."""
    recorded: list[tuple] = []
    set_alert_active = db.set_alert_active
    log_alert_event = db.log_alert_event

    def fake_set_alert_active(conn, alert_id, *, is_active):
        recorded.append(("set_active", is_active))
        set_alert_active(conn, alert_id, is_active=is_active)

    def fake_log_alert_event(conn, alert_id, triggered_at):
        recorded.append(("event", triggered_at))
        log_alert_event(conn, alert_id, triggered_at)

    class FakeMetrics:
        def increment(self, name):
            recorded.append(("metric", name))

    monkeypatch.setattr(alerts, "metrics", FakeMetrics())
    monkeypatch.setattr(db, "set_alert_active", fake_set_alert_active)
    monkeypatch.setattr(db, "log_alert_event", fake_log_alert_event)
    monkeypatch.setattr(telegram, "send_telegram_message", lambda text: recorded.append(("send", text)))
    return recorded


def _fix_at(conn, position, minute, now):
    result = record_fetch(conn, [make_item("tag-1", make_location(*position, minutes_later(minute)))])
    check_alerts(conn, result.moved_device_ids, now)


def test_enter_alert_writes_and_sends_across_enter_leave_and_cooldown(conn, calls):
    record_fetch(conn, [make_item("tag-1")])
    create_alert(conn, "tag-1", "enter", 100)

    _fix_at(conn, FAR, 0, T0)
    assert calls == []

    _fix_at(conn, HOME, 1, T0)
    assert calls == [
        ("set_active", True),
        ("event", T0.isoformat()),
        ("metric", "enter_triggered"),
        ("send", ENTER_TEXT),
    ]

    calls.clear()
    _fix_at(conn, FAR, 2, T0 + timedelta(seconds=60))
    assert calls == [("set_active", False)]

    calls.clear()
    _fix_at(conn, HOME, 3, T0 + timedelta(seconds=299))
    assert calls == []
    assert alerts_for_device(conn, "tag-1")[0]["is_active"] == 0

    retry_at = T0 + timedelta(seconds=300)
    _fix_at(conn, NEAR_HOME, 4, retry_at)
    assert calls == [
        ("set_active", True),
        ("event", retry_at.isoformat()),
        ("metric", "enter_triggered"),
        ("send", ENTER_TEXT),
    ]
    assert alerts_for_device(conn, "tag-1")[0]["triggered_at"] == retry_at.isoformat()


def test_exit_alert_tracks_entry_silently_then_notifies_on_leaving(conn, calls):
    record_fetch(conn, [make_item("tag-1")])
    create_alert(conn, "tag-1", "exit", 100)

    _fix_at(conn, FAR, 0, T0)
    assert calls == []

    _fix_at(conn, HOME, 1, T0)
    assert calls == [("set_active", True)]
    assert alerts_for_device(conn, "tag-1")[0]["triggered_at"] is None

    calls.clear()
    leave_at = T0 + timedelta(seconds=1)
    _fix_at(conn, FAR, 2, leave_at)
    assert calls == [
        ("set_active", False),
        ("event", leave_at.isoformat()),
        ("metric", "exit_triggered"),
        ("send", EXIT_TEXT),
    ]


def test_movement_alert_refires_exactly_at_the_cooldown_boundary(conn, calls):
    record_fetch(conn, [make_item("tag-1", make_location(52.5, 13.4, minutes_later(0)))])
    create_alert(conn, "tag-1", "movement", 100)
    db.log_alert_event(
        conn, alerts_for_device(conn, "tag-1")[0]["id"], (T0 - timedelta(seconds=300)).isoformat()
    )
    calls.clear()

    _fix_at(conn, (52.6, 13.4), 1, T0)

    assert calls[0] == ("event", T0.isoformat())
    assert calls[1] == ("metric", "movement_triggered")
    assert calls[2][0] == "send"
