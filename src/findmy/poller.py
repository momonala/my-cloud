"""Background fetch loop for the Flask API.

Runs `fetch_devices()` and `fetch_airtags()` once a minute on a daemon thread
and records the results via `src.findmy.db.record_fetch`, so HTTP requests never wait
on a live Apple round trip -- they just read whatever's already in SQLite.

This reuses whatever session and tracker keys are already cached in
`.icloud_session/`. If that cache doesn't exist yet, run `uv run mycloud
airtags` (or `devices`) once at the console first -- first-time 2FA and the
Keychain prompt can't be satisfied from a background thread.
"""

import logging
import threading
from datetime import UTC
from datetime import datetime
from enum import StrEnum

from src.core.db import connection
from src.core.db import get_connection
from src.core.telemetry import metrics
from src.findmy.airtags import fetch_airtags
from src.findmy.alerts import check_alerts
from src.findmy.db import NO_COUNTS
from src.findmy.db import record_fetch
from src.findmy.devices import fetch_devices

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 60
MAX_BACKOFF_SECONDS = 15 * 60
# Past this many consecutive failures the cause is no longer a passing network
# blip (an expired session, revoked credentials), so it's logged as an error.
_PERSISTENT_FAILURE_THRESHOLD = 3


class PollerPhase(StrEnum):
    FAILING = "failing"
    PERSISTENTLY_FAILING = "persistently_failing"


class PollerState:
    """Poll health shared between the poller thread and the web layer."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._last_success_at: datetime | None = None

    @property
    def last_success_at(self) -> datetime | None:
        with self._lock:
            return self._last_success_at

    def record_poll(self, now: datetime) -> None:
        with self._lock:
            self._last_success_at = now

    def record_recovery(self) -> None:
        with self._lock:
            self._consecutive_failures = 0

    def record_failure(self) -> tuple[PollerPhase, int]:
        with self._lock:
            self._consecutive_failures += 1
            failures = self._consecutive_failures
        if failures >= _PERSISTENT_FAILURE_THRESHOLD:
            return PollerPhase.PERSISTENTLY_FAILING, failures
        return PollerPhase.FAILING, failures

    def backoff_seconds(self) -> int:
        """Delay before the next cycle: the normal interval, doubling while failing."""
        with self._lock:
            failures = self._consecutive_failures
        if failures == 0:
            return POLL_INTERVAL_SECONDS
        doubled: int = POLL_INTERVAL_SECONDS * 2**failures
        return min(doubled, MAX_BACKOFF_SECONDS)


poller_state = PollerState()


def _poll_once() -> None:
    with metrics.timed("duration"):
        items = fetch_devices() + fetch_airtags()
        with connection() as conn:
            result = record_fetch(conn, items)
            # Same connection/transaction as record_fetch, so a crash between the
            # two can't leave history written but that cycle's alerts unevaluated.
            check_alerts(conn, result.moved_device_ids)
    item_counts = result.counts.get("item", NO_COUNTS)
    device_counts = result.counts.get("device", NO_COUNTS)
    logger.info(
        "[%d/%d] items  [%d/%d] devices",
        item_counts.written,
        item_counts.fetched,
        device_counts.written,
        device_counts.fetched,
    )


def run_forever(stop_event: threading.Event, state: PollerState) -> None:
    """Poll immediately, then on an interval until `stop_event` is set.

    Failures back off exponentially up to `MAX_BACKOFF_SECONDS` rather than
    retrying at full rate: an expired iCloud session fails identically every
    time, and hammering Apple once a minute forever helps nobody.
    """
    # SQLite checkpoints and deletes the WAL whenever its last connection closes;
    # an idle connection held for the poller's lifetime stops that once a minute.
    wal_keeper = None
    try:
        while not stop_event.is_set():
            try:
                _poll_once()
                state.record_poll(datetime.now(UTC))
                if wal_keeper is None:
                    wal_keeper = get_connection()
                state.record_recovery()
                metrics.gauge("consecutive_failures", 0)
            except Exception:
                # A network blip or expired session shouldn't kill the poller or the
                # API -- just keep serving the last-known data and try again later.
                phase, failures = state.record_failure()
                metrics.increment("failure")
                metrics.gauge("consecutive_failures", failures)
                match phase:
                    case PollerPhase.PERSISTENTLY_FAILING:
                        logger.exception(
                            "Poll cycle failed %d times in a row; the session may need re-authenticating "
                            "at the console",
                            failures,
                        )
                    case PollerPhase.FAILING:
                        logger.warning("Poll cycle failed; keeping last known data", exc_info=True)
            stop_event.wait(state.backoff_seconds())
    finally:
        if wal_keeper is not None:
            wal_keeper.close()


def start_background_poller() -> threading.Event:
    """Start the poller on a daemon thread and return its stop event."""
    stop_event = threading.Event()
    threading.Thread(target=run_forever, args=(stop_event, poller_state), daemon=True).start()
    return stop_event
