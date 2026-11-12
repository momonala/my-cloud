"""Tests for the Find My JSON routes (src/web/findmy/api.py) and its page.

Uses `create_app(start_poller=False)` against a temp DB (monkeypatched via
`src.core.db.DB_PATH`) so routes are exercised without any live Apple fetch,
and data is seeded directly through `src.findmy.db.record_fetch`. See
tests/conftest.py for the `client`/`seed` fixtures.
"""

from datetime import UTC
from datetime import datetime

import pytest

import src.web.auth as auth
import src.web.findmy.api as findmy_api
from src.core.env import HOME_LATITUDE
from src.core.env import HOME_LONGITUDE
from tests.conftest import make_item
from tests.conftest import make_location
from tests.conftest import minutes_later


def _create_alert(client, alert_type="movement"):
    response = client.post(
        "/api/findmy/alerts", json={"device_id": "tag-1", "alert_type": alert_type, "threshold_m": 100}
    )
    return response.get_json()["id"]


def test_page_loads(client):
    response = client.get("/findmy")
    assert response.status_code == 200
    assert b"Find My dashboard" in response.data


def test_config_reports_telegram_configured_only_when_both_are_set(client, monkeypatch):
    monkeypatch.setattr(findmy_api, "TELEGRAM_API_TOKEN", "token")
    monkeypatch.setattr(findmy_api, "TELEGRAM_CHAT_ID", "")
    assert client.get("/api/findmy/config").get_json() == {
        "home_latitude": HOME_LATITUDE,
        "home_longitude": HOME_LONGITUDE,
        "telegram_configured": False,
    }

    monkeypatch.setattr(findmy_api, "TELEGRAM_CHAT_ID", "chat")
    assert client.get("/api/findmy/config").get_json()["telegram_configured"] is True


def test_get_unknown_location_is_404(client):
    assert client.get("/api/findmy/locations/does-not-exist").status_code == 404


def test_history_limit_param(client, seed):
    seed(make_item("tag-1", make_location(52.5, 13.4)))
    seed(make_item("tag-1", make_location(52.6, 13.4, minutes_later(1))))

    assert len(client.get("/api/findmy/locations/tag-1/history?limit=1").get_json()) == 1


def test_history_for_unknown_device_is_404(client):
    assert client.get("/api/findmy/locations/does-not-exist/history").status_code == 404


def test_status_falls_back_to_the_last_device_change_before_any_poll(client, seed):
    assert client.get("/api/findmy/status").get_json() == {"last_updated": None}

    seed(make_item("tag-1", make_location(52.5, 13.4)))

    assert client.get("/api/findmy/status").get_json()["last_updated"] is not None


def test_status_prefers_the_last_poll_over_the_last_device_change(client, seed, monkeypatch):
    import src.findmy.poller as poller

    seed(make_item("tag-1", make_location(52.5, 13.4)))
    state = poller.PollerState()
    state.record_poll(datetime(2030, 1, 1, tzinfo=UTC))
    monkeypatch.setattr(findmy_api, "poller_state", state)

    assert client.get("/api/findmy/status").get_json() == {"last_updated": "2030-01-01T00:00:00+00:00"}


def test_distance_is_computed_server_side(client, seed):
    """The dashboard reads `distance_m` rather than recomputing a haversine."""
    seed(make_item("at-home", make_location(HOME_LATITUDE, HOME_LONGITUDE)))
    seed(make_item("no-fix"))

    rows = {row["id"]: row for row in client.get("/api/findmy/locations").get_json()}
    assert rows["at-home"]["distance_m"] == 0
    assert rows["no-fix"]["distance_m"] is None


def test_default_icon_used_for_known_apple_kinds(client, seed):
    seed(make_item("phone", make_location(52.5, 13.4), kind="iPhone", source="device"))

    assert client.get("/api/findmy/locations/phone").get_json()["icon"] == "📱"


# --- PUT /locations/<id>/icon --------------------------------------------------


def test_put_icon_sets_it_and_returns_the_full_device(client, seed):
    seed(make_item("tag-1", make_location(52.5, 13.4)))

    body = client.put("/api/findmy/locations/tag-1/icon", json={"emoji": "🚲"}).get_json()

    # Same shape as GET, so a client can re-render straight from the response.
    assert body["id"] == "tag-1"
    assert body["icon"] == "🚲"
    assert body["latitude"] == 52.5
    assert client.get("/api/findmy/locations/tag-1").get_json()["icon"] == "🚲"


@pytest.mark.parametrize("cleared", [None, "   "])
def test_put_icon_with_null_or_blank_emoji_clears_it(client, seed, cleared):
    seed(make_item("tag-1", make_location(52.5, 13.4)))
    client.put("/api/findmy/locations/tag-1/icon", json={"emoji": "🚲"})

    client.put("/api/findmy/locations/tag-1/icon", json={"emoji": cleared})

    assert client.get("/api/findmy/locations/tag-1").get_json()["icon"] is None


def test_put_icon_for_unknown_device_is_404(client):
    assert client.put("/api/findmy/locations/does-not-exist/icon", json={"emoji": "🚲"}).status_code == 404


@pytest.mark.parametrize(
    "body",
    [None, {"emoji": 42}, {"emoji": "x" * 17}, {"emoji": "a\x00b"}],
    ids=["missing-body", "non-string", "overlong", "control-character"],
)
def test_put_icon_rejects_junk(client, seed, body):
    seed(make_item("tag-1", make_location(52.5, 13.4)))

    response = client.put("/api/findmy/locations/tag-1/icon", json=body)

    assert response.status_code == 400
    assert client.get("/api/findmy/locations/tag-1").get_json()["icon"] is None


# --- write token ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body", "ok_status"),
    [
        ("put", "/api/findmy/locations/tag-1/icon", {"emoji": "🚲"}, 200),
        (
            "post",
            "/api/findmy/alerts",
            {"device_id": "tag-1", "alert_type": "movement", "threshold_m": 100},
            201,
        ),
        ("put", "/api/findmy/alerts/{alert_id}", {"alert_type": "movement", "threshold_m": 100}, 200),
        ("delete", "/api/findmy/alerts/{alert_id}", None, 204),
    ],
)
def test_writes_require_the_token_when_configured(client, seed, monkeypatch, method, path, body, ok_status):
    seed(make_item("tag-1", make_location(52.5, 13.4)))
    url = path.format(alert_id=_create_alert(client))
    monkeypatch.setattr(auth, "API_WRITE_TOKEN", "s3cret")
    send = getattr(client, method)

    assert send(url, json=body).status_code == 401
    assert send(url, json=body, headers={"X-Api-Token": "s3cret"}).status_code == ok_status


def test_reads_are_open_even_when_a_write_token_is_configured(client, seed, monkeypatch):
    seed(make_item("tag-1", make_location(52.5, 13.4)))
    monkeypatch.setattr(auth, "API_WRITE_TOKEN", "s3cret")

    assert client.get("/api/findmy/locations").status_code == 200
    assert client.get("/api/findmy/alerts").status_code == 200


# --- /alerts --------------------------------------------------------------


def test_post_alert_creates_it(client, seed):
    seed(make_item("tag-1", make_location(52.5, 13.4)))

    response = client.post(
        "/api/findmy/alerts", json={"device_id": "tag-1", "alert_type": "movement", "threshold_m": 150}
    )

    assert response.status_code == 201
    body = response.get_json()
    assert body["device_id"] == "tag-1"
    assert body["device_name"] == "Item tag-1"
    assert body["alert_type"] == "movement"
    assert body["threshold_m"] == 150
    assert body["is_active"] is False
    assert body["triggered_at"] is None
    assert body["anchor_lat"] is None
    assert body["anchor_lon"] is None
    assert len(client.get("/api/findmy/alerts").get_json()) == 1


def test_post_enter_alert_with_current_anchor_snapshots_the_devices_location(client, seed):
    seed(make_item("tag-1", make_location(52.5, 13.4)))

    response = client.post(
        "/api/findmy/alerts",
        json={"device_id": "tag-1", "alert_type": "enter", "threshold_m": 100, "anchor": "current"},
    )

    body = response.get_json()
    assert response.status_code == 201
    assert body["anchor_lat"] == 52.5
    assert body["anchor_lon"] == 13.4


def test_post_alert_with_current_anchor_and_no_fix_yet_is_400(client, seed):
    seed(make_item("tag-1"))  # device known, no fix yet

    response = client.post(
        "/api/findmy/alerts",
        json={"device_id": "tag-1", "alert_type": "enter", "threshold_m": 100, "anchor": "current"},
    )

    assert response.status_code == 400


@pytest.mark.parametrize("anchor", ["home", "current"])
def test_post_alert_for_unknown_device_is_404(client, anchor):
    response = client.post(
        "/api/findmy/alerts",
        json={"device_id": "does-not-exist", "alert_type": "enter", "threshold_m": 100, "anchor": anchor},
    )

    assert response.status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        None,
        {"device_id": "tag-1", "alert_type": "bogus", "threshold_m": 100},
        {"device_id": "tag-1", "alert_type": "movement", "threshold_m": 0},
        {"device_id": "tag-1", "alert_type": "movement", "threshold_m": "far"},
        {"device_id": "tag-1", "alert_type": "enter", "threshold_m": 100, "anchor": "bogus"},
    ],
    ids=["missing-body", "alert-type", "non-positive-threshold", "non-numeric-threshold", "anchor"],
)
def test_post_alert_rejects_junk(client, seed, body):
    seed(make_item("tag-1", make_location(52.5, 13.4)))

    assert client.post("/api/findmy/alerts", json=body).status_code == 400


def test_put_alert_updates_it(client, seed):
    seed(make_item("tag-1", make_location(52.5, 13.4)))
    alert_id = _create_alert(client)

    response = client.put(f"/api/findmy/alerts/{alert_id}", json={"alert_type": "exit", "threshold_m": 250})

    assert response.status_code == 200
    body = response.get_json()
    assert body["id"] == alert_id
    assert body["device_id"] == "tag-1"
    assert body["alert_type"] == "exit"
    assert body["threshold_m"] == 250
    assert body["anchor_lat"] is None
    assert body["anchor_lon"] is None


def test_put_alert_with_current_anchor_snapshots_the_devices_location(client, seed):
    seed(make_item("tag-1", make_location(52.5, 13.4)))
    alert_id = _create_alert(client, "enter")

    response = client.put(
        f"/api/findmy/alerts/{alert_id}",
        json={"alert_type": "enter", "threshold_m": 100, "anchor": "current"},
    )

    body = response.get_json()
    assert response.status_code == 200
    assert body["anchor_lat"] == 52.5
    assert body["anchor_lon"] == 13.4


def test_put_alert_with_current_anchor_and_no_fix_yet_is_400(client, seed):
    seed(make_item("tag-1"))  # device known, no fix yet
    alert_id = _create_alert(client, "enter")

    response = client.put(
        f"/api/findmy/alerts/{alert_id}",
        json={"alert_type": "enter", "threshold_m": 100, "anchor": "current"},
    )

    assert response.status_code == 400


def test_put_unknown_alert_is_404(client):
    response = client.put("/api/findmy/alerts/999", json={"alert_type": "movement", "threshold_m": 100})

    assert response.status_code == 404


def test_delete_alert_removes_it(client, seed):
    seed(make_item("tag-1", make_location(52.5, 13.4)))
    alert_id = _create_alert(client)

    response = client.delete(f"/api/findmy/alerts/{alert_id}")

    assert response.status_code == 204
    assert client.get("/api/findmy/alerts").get_json() == []


def test_delete_unknown_alert_is_404(client):
    assert client.delete("/api/findmy/alerts/999").status_code == 404
