"""Tests for src/findmy/alerts.py's check_alerts(): movement alerts, custom anchors,
and failure isolation. The enter/exit edge trigger lives in tests/test_alert_state.py.
"""

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from unittest.mock import patch

from src.core.env import HOME_LATITUDE
from src.core.env import HOME_LONGITUDE
from src.findmy.alerts import check_alerts
from src.findmy.db import alerts_for_device
from src.findmy.db import create_alert
from src.findmy.db import record_fetch
from src.findmy.telegram import TelegramSendError
from tests.conftest import make_item
from tests.conftest import make_location
from tests.conftest import minutes_later


@patch("src.findmy.alerts.send_movement_alert")
def test_movement_alert_fires_and_notifies_when_the_jump_exceeds_the_threshold(mock_send, conn):
    record_fetch(conn, [make_item("tag-1", make_location(52.5, 13.4))])
    create_alert(conn, "tag-1", "movement", 100)

    # ~11km jump between fixes, well over the 100m threshold.
    result = record_fetch(conn, [make_item("tag-1", make_location(52.6, 13.4, minutes_later(1)))])
    check_alerts(conn, result.moved_device_ids, datetime.now(UTC))

    assert alerts_for_device(conn, "tag-1")[0]["triggered_at"] is not None
    mock_send.assert_called_once()
    assert mock_send.call_args.args[0]["device_id"] == "tag-1"


@patch("src.findmy.alerts.send_movement_alert")
def test_movement_alert_does_not_fire_for_a_move_under_the_threshold(mock_send, conn):
    record_fetch(conn, [make_item("tag-1", make_location(52.5, 13.4))])
    create_alert(conn, "tag-1", "movement", 100)

    # ~1.1m move -- under the 100m threshold.
    result = record_fetch(conn, [make_item("tag-1", make_location(52.50001, 13.4, minutes_later(1)))])
    check_alerts(conn, result.moved_device_ids, datetime.now(UTC))

    assert alerts_for_device(conn, "tag-1")[0]["triggered_at"] is None
    mock_send.assert_not_called()


def test_movement_alert_skipped_on_a_devices_first_ever_fix(conn):
    record_fetch(conn, [make_item("tag-1")])  # device known, no fix yet
    create_alert(conn, "tag-1", "movement", 100)

    result = record_fetch(conn, [make_item("tag-1", make_location(52.5, 13.4))])
    check_alerts(conn, result.moved_device_ids, datetime.now(UTC))

    assert alerts_for_device(conn, "tag-1")[0]["triggered_at"] is None


def test_movement_alert_does_not_refire_within_the_cooldown(conn):
    record_fetch(conn, [make_item("tag-1", make_location(52.5, 13.4))])
    create_alert(conn, "tag-1", "movement", 100)

    first = record_fetch(conn, [make_item("tag-1", make_location(52.6, 13.4, minutes_later(1)))])
    check_alerts(conn, first.moved_device_ids, datetime.now(UTC))
    first_triggered_at = alerts_for_device(conn, "tag-1")[0]["triggered_at"]

    second = record_fetch(
        conn, [make_item("tag-1", make_location(52.5, 13.4, minutes_later(1) + timedelta(seconds=1)))]
    )
    check_alerts(conn, second.moved_device_ids, datetime.now(UTC))

    assert alerts_for_device(conn, "tag-1")[0]["triggered_at"] == first_triggered_at


def test_enter_alert_measures_from_a_custom_anchor_not_home(conn):
    anchor_lat, anchor_lon = HOME_LATITUDE + 2, HOME_LONGITUDE
    record_fetch(conn, [make_item("tag-1", make_location(HOME_LATITUDE, HOME_LONGITUDE))])
    create_alert(conn, "tag-1", "enter", 100, anchor_lat=anchor_lat, anchor_lon=anchor_lon)

    near_home = record_fetch(
        conn, [make_item("tag-1", make_location(HOME_LATITUDE + 0.001, HOME_LONGITUDE, minutes_later(1)))]
    )
    check_alerts(conn, near_home.moved_device_ids, datetime.now(UTC))
    assert alerts_for_device(conn, "tag-1")[0]["is_active"] == 0

    at_anchor = record_fetch(
        conn, [make_item("tag-1", make_location(anchor_lat, anchor_lon, minutes_later(2)))]
    )
    check_alerts(conn, at_anchor.moved_device_ids, datetime.now(UTC))
    assert alerts_for_device(conn, "tag-1")[0]["is_active"] == 1


@patch("src.findmy.alerts.send_movement_alert", side_effect=TelegramSendError("down"))
def test_a_failed_telegram_send_does_not_stop_the_rest_of_the_cycle(mock_send, conn):
    record_fetch(
        conn, [make_item("tag-1", make_location(52.5, 13.4)), make_item("tag-2", make_location(52.5, 13.4))]
    )
    create_alert(conn, "tag-1", "movement", 100)
    create_alert(conn, "tag-2", "movement", 100)

    result = record_fetch(
        conn,
        [
            make_item("tag-1", make_location(52.6, 13.4, minutes_later(1))),
            make_item("tag-2", make_location(52.6, 13.4, minutes_later(1))),
        ],
    )
    check_alerts(conn, result.moved_device_ids, datetime.now(UTC))

    assert alerts_for_device(conn, "tag-1")[0]["triggered_at"] is not None
    assert alerts_for_device(conn, "tag-2")[0]["triggered_at"] is not None
