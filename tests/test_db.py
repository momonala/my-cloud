"""Tests for src/findmy/db.py's write rules.

Lookups and alert CRUD are exercised through the routes in tests/test_findmy_api.py
and through check_alerts in tests/test_alert_state.py; what is pinned here is
when a fetch writes history and device rows, and that re-initialising keeps data.
"""

from dataclasses import replace

from src.findmy.db import all_latest_locations
from src.findmy.db import history_for
from src.findmy.db import latest_location_for
from src.findmy.db import record_fetch
from tests.conftest import make_item
from tests.conftest import make_location
from tests.conftest import minutes_later


def test_device_without_location_is_recorded_without_history(conn):
    record_fetch(conn, [make_item("no-fix")])

    row = latest_location_for(conn, "no-fix")
    assert row["id"] == "no-fix"
    assert row["latitude"] is None
    assert history_for(conn, "no-fix") == []


def test_repeated_identical_fix_does_not_grow_history(conn):
    record_fetch(conn, [make_item("tag-1", make_location(52.5, 13.4))])
    result = record_fetch(conn, [make_item("tag-1", make_location(52.5, 13.4))])

    assert len(history_for(conn, "tag-1")) == 1
    assert result.moved_device_ids == set()


def test_changed_fix_adds_a_history_row(conn):
    record_fetch(conn, [make_item("tag-1", make_location(52.5, 13.4))])
    result = record_fetch(conn, [make_item("tag-1", make_location(52.6, 13.4, minutes_later(1)))])

    history = history_for(conn, "tag-1")
    assert len(history) == 2
    assert history[0]["latitude"] == 52.6  # newest first
    assert result.moved_device_ids == {"tag-1"}


def test_all_latest_locations_includes_devices_without_a_fix(conn):
    record_fetch(conn, [make_item("tag-1", make_location(52.5, 13.4)), make_item("no-fix")])

    rows = {row["id"]: row for row in all_latest_locations(conn)}
    assert rows["tag-1"]["latitude"] == 52.5
    assert rows["no-fix"]["latitude"] is None


def test_history_respects_since(conn):
    record_fetch(conn, [make_item("tag-1", make_location(52.5, 13.4))])
    record_fetch(conn, [make_item("tag-1", make_location(52.6, 13.4, minutes_later(10)))])

    recent = history_for(conn, "tag-1", since=minutes_later(5).isoformat())
    assert [row["latitude"] for row in recent] == [52.6]


def test_init_db_is_idempotent(conn, tmp_path):
    """`init_db` runs on every `mycloud serve` boot, so it must keep existing history."""
    from src.core.db import init_db

    record_fetch(conn, [make_item("tag-1", make_location(52.5, 13.4))])
    init_db(tmp_path / "findmy.db")

    assert latest_location_for(conn, "tag-1")["latitude"] == 52.5


def _device_updated_at(conn, device_id):
    return conn.execute("SELECT updated_at FROM devices WHERE id = ?", (device_id,)).fetchone()["updated_at"]


def test_unchanged_metadata_does_not_rewrite_the_device_row(conn):
    record_fetch(conn, [make_item("tag-1", make_location(52.5, 13.4))])
    first = _device_updated_at(conn, "tag-1")

    record_fetch(conn, [make_item("tag-1", make_location(52.5, 13.4))])

    assert _device_updated_at(conn, "tag-1") == first


def test_battery_change_rewrites_the_device_row(conn):
    item = make_item("tag-1", make_location(52.5, 13.4))
    record_fetch(conn, [item])
    first = _device_updated_at(conn, "tag-1")

    record_fetch(conn, [replace(item, battery_level="Low")])

    assert _device_updated_at(conn, "tag-1") > first
