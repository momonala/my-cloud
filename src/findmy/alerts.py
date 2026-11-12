"""Evaluate configured alerts against freshly-written location fixes.

Called from src/findmy/poller.py right after src.findmy.db.record_fetch, inside the same
transaction. Only devices that got a new `location_history` row this cycle are
checked -- for every alert type, nothing about the movement delta or the
distance to the anchor changes without a new fix.

Three alert types: `movement` fires when consecutive fixes are more than
`threshold_m` apart; `enter` and `exit` fire when the device crosses into or
out of `threshold_m` of its anchor point -- `(anchor_lat, anchor_lon)` if set,
else the configured home coordinates.

`enter`/`exit` are edge-triggered by next_state() over an AlertState read from
the alert row: `inside` is `alerts.is_active` ("the device is currently inside
this alert's radius", regardless of type) and `cooldown_until` follows from the
last `alert_events` row. Both types track `inside` so each can detect its own
transition; the opposite transition updates it silently so the next real
crossing is still detected.

All three types are gated by ALERT_COOLDOWN_S. A suppressed `enter`/`exit`
transition leaves the state untouched and so is retried on the next cycle,
landing late rather than being lost.

Delivery is in-app (the dashboard reads `is_active`/`triggered_at` off
GET /alerts) plus an optional Telegram push from src/findmy/telegram.py.
"""

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import replace
from datetime import datetime
from datetime import timedelta
from typing import Literal

import src.findmy.db as db
from src.core.config import HOME_LATITUDE
from src.core.config import HOME_LONGITUDE
from src.core.telemetry import metrics
from src.findmy.telegram import TelegramSendError
from src.findmy.telegram import send_enter_alert
from src.findmy.telegram import send_exit_alert
from src.findmy.telegram import send_movement_alert
from src.findmy.tracking import haversine_m

logger = logging.getLogger(__name__)

# Minimum time between two notifications for the *same* alert, regardless of
# type -- keeps a device flapping across a radius boundary, or drifting back
# and forth past a movement threshold, from notifying every poll cycle.
ALERT_COOLDOWN_S = 300

RadiusAlertType = Literal["enter", "exit"]


@dataclass(frozen=True)
class AlertState:
    """Whether the device was last seen inside the alert's radius, and when the alert may next fire."""

    inside: bool
    cooldown_until: datetime | None

    @classmethod
    def from_row(cls, alert: sqlite3.Row) -> "AlertState":
        triggered_at = alert["triggered_at"]
        cooldown_until = (
            None
            if triggered_at is None
            else datetime.fromisoformat(triggered_at) + timedelta(seconds=ALERT_COOLDOWN_S)
        )
        return cls(inside=bool(alert["is_active"]), cooldown_until=cooldown_until)

    def cooldown_elapsed(self, now: datetime) -> bool:
        return self.cooldown_until is None or now >= self.cooldown_until


@dataclass(frozen=True)
class Transition:
    state: AlertState
    notify: bool


def next_state(state: AlertState, inside: bool, now: datetime, *, alert_type: RadiusAlertType) -> Transition:
    """Advance an `enter`/`exit` alert on a new fix that is `inside` its radius or not."""
    if inside == state.inside:
        return Transition(state, notify=False)
    if inside != (alert_type == "enter"):
        return Transition(replace(state, inside=inside), notify=False)
    if not state.cooldown_elapsed(now):
        return Transition(state, notify=False)
    return Transition(
        AlertState(inside=inside, cooldown_until=now + timedelta(seconds=ALERT_COOLDOWN_S)), notify=True
    )


def check_alerts(conn: sqlite3.Connection, moved_device_ids: set[str], now: datetime) -> None:
    """Re-evaluate every alert belonging to a device that just moved."""
    for device_id in moved_device_ids:
        device_alerts = db.alerts_for_device(conn, device_id)
        if not device_alerts:
            continue

        fixes = db.history_for(conn, device_id, limit=2)  # newest first
        if not fixes:
            continue
        current = fixes[0]
        previous = fixes[1] if len(fixes) > 1 else None

        for alert in device_alerts:
            if alert["alert_type"] == "movement":
                _check_movement(conn, alert, previous, current, now)
            else:
                _check_radius_crossing(conn, alert, current, now)


def _check_movement(
    conn: sqlite3.Connection,
    alert: sqlite3.Row,
    previous: sqlite3.Row | None,
    current: sqlite3.Row,
    now: datetime,
) -> None:
    if previous is None:
        return  # first-ever fix for this device: nothing to compare against
    moved_m = haversine_m(
        previous["latitude"], previous["longitude"], current["latitude"], current["longitude"]
    )
    if moved_m > alert["threshold_m"] and AlertState.from_row(alert).cooldown_elapsed(now):
        db.log_alert_event(conn, alert["id"], now.isoformat())
        metrics.increment("movement_triggered")
        _notify(alert["id"], lambda: send_movement_alert(alert, moved_m))


def _check_radius_crossing(
    conn: sqlite3.Connection, alert: sqlite3.Row, current: sqlite3.Row, now: datetime
) -> None:
    anchor_lat = alert["anchor_lat"] if alert["anchor_lat"] is not None else HOME_LATITUDE
    anchor_lon = alert["anchor_lon"] if alert["anchor_lon"] is not None else HOME_LONGITUDE
    distance_m = haversine_m(anchor_lat, anchor_lon, current["latitude"], current["longitude"])
    inside = distance_m <= alert["threshold_m"]

    state = AlertState.from_row(alert)
    transition = next_state(state, inside, now, alert_type=alert["alert_type"])

    if transition.state.inside != state.inside:
        db.set_alert_active(conn, alert["id"], is_active=transition.state.inside)
    if transition.notify:
        db.log_alert_event(conn, alert["id"], now.isoformat())
        metrics.increment(f"{alert['alert_type']}_triggered")
        send = send_enter_alert if alert["alert_type"] == "enter" else send_exit_alert
        _notify(alert["id"], lambda: send(alert))


def _notify(alert_id: int, send: Callable[[], None]) -> None:
    """Best-effort Telegram push -- a down or misconfigured bot must not stop the
    rest of this cycle's alerts from being evaluated and recorded in-app."""
    try:
        send()
    except TelegramSendError as e:
        metrics.increment("telegram_failed")
        logger.warning("Telegram notification failed for alert %s: %s", alert_id, e)
