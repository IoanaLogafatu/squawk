"""
tests/test_concorde_service.py

The Concorde service — a single-writer process — and reading what it
publishes.

The loop itself is refresh(), write_state() and a sleep, so the tests
drive refresh() directly and move time by backdating the pass rather than
by waiting twenty minutes.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta

import pytest

from schemas.aircraft import ensure_utc, now_utc
from services import SERVICES
from services.base import ServiceAlreadyRunning, is_running, read_state
from services.concorde.service import (
    CRUISE_FEET,
    PASS_RANGE_NM,
    START_FEET,
    ConcordeService,
    distance_nm,
)

OBSERVER = (52.0, -1.0)


def _backdate(service: ConcordeService, minutes: float) -> None:
    """Pretend the pass in progress started this many minutes ago."""
    service.flight["start_time"] = (now_utc() - timedelta(minutes=minutes)).isoformat()


def test_concorde_is_registered():
    assert SERVICES["concorde"] is ConcordeService


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------

def test_what_is_published_is_what_consumers_read(app):
    service = ConcordeService("concorde", app.service("concorde"), app)
    published = service.refresh()
    service.write_state(published)

    assert read_state(app.data_dir, "concorde") == published
    assert service.state_path == app.data_dir / "services" / "concorde" / "state.json"


def test_each_refresh_is_stamped_with_when_it_was_computed(app):
    service = ConcordeService("concorde", app.service("concorde"), app)

    before = now_utc()
    state  = service.refresh()

    observed_at = ensure_utc(datetime.fromisoformat(state["observed_at"]))
    assert before <= observed_at <= now_utc()


def test_reading_before_the_service_has_run_finds_nothing(app):
    assert read_state(app.data_dir, "concorde") is None


def test_an_unreadable_state_file_reads_as_nothing(app, concorde):
    concorde.state_path.write_text("{ truncated")

    assert read_state(app.data_dir, "concorde") is None


def test_readers_never_see_a_partial_write(app, concorde):
    """
    Single writer, many readers, no lock: atomic renames are what make it
    safe, and a reader racing a busy writer must only ever see whole files.
    """
    stop     = threading.Event()
    failures = []

    def writer():
        while not stop.is_set():
            concorde.write_state(concorde.refresh())

    def reader():
        for _ in range(500):
            if read_state(app.data_dir, "concorde") is None:
                failures.append("read a partial or missing state file")

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        reader()
    finally:
        stop.set()
        thread.join()

    assert not failures


# ---------------------------------------------------------------------------
# Sole writer
# ---------------------------------------------------------------------------

def test_a_second_copy_of_a_running_service_refuses_to_start(app):
    first  = ConcordeService("concorde", app.service("concorde"), app)
    second = ConcordeService("concorde", app.service("concorde"), app)

    with first._sole_writer():
        assert is_running(app.data_dir, "concorde")
        with pytest.raises(ServiceAlreadyRunning, match="already running"):
            second.run()

    assert not is_running(app.data_dir, "concorde")


def test_a_service_that_never_ran_is_not_running(app):
    assert not is_running(app.data_dir, "concorde")


# ---------------------------------------------------------------------------
# Passes
# ---------------------------------------------------------------------------

def test_refreshes_continue_the_same_pass(app):
    service = ConcordeService("concorde", app.service("concorde"), app)

    first  = service.refresh()
    second = service.refresh()

    assert first["pass"] == second["pass"]


def test_a_completed_pass_is_replaced(app):
    service = ConcordeService("concorde", app.service("concorde"), app)
    service.refresh()
    old_start = service.flight["start_time"]

    _backdate(service, minutes=120)
    service.refresh()

    assert service.flight["start_time"] != old_start
    assert abs(service.refresh()["position"]["distance_nm"] - PASS_RANGE_NM) < 0.5


def test_a_restarted_service_resumes_the_pass_in_progress(app, concorde):
    _backdate(concorde, minutes=4)
    concorde.write_state(concorde.refresh())

    # A restart is just a new object over the same folder.
    restarted = ConcordeService("concorde", app.service("concorde"), app)

    assert restarted.flight == concorde.flight


def test_a_finished_pass_is_not_resumed(app, concorde):
    _backdate(concorde, minutes=120)
    # Published as it stands, without the refresh that would replace it.
    concorde.write_state({"observed_at": now_utc().isoformat(), "position": {},
                          "pass": concorde.flight})

    assert ConcordeService("concorde", app.service("concorde"), app).flight is None


def test_a_pass_around_a_different_observer_is_not_resumed(app, concorde):
    app.observer_latitude = 40.0

    assert ConcordeService("concorde", app.service("concorde"), app).flight is None


def test_an_unreadable_state_file_starts_a_fresh_pass(app, concorde):
    concorde.state_path.write_text("{ truncated")

    service = ConcordeService("concorde", app.service("concorde"), app)
    assert service.flight is None
    assert service.refresh()["pass"] is not None


# ---------------------------------------------------------------------------
# The flight itself
# ---------------------------------------------------------------------------

def test_she_spawns_the_full_pass_range_out(app):
    position = ConcordeService("concorde", app.service("concorde"), app).refresh()["position"]

    assert abs(position["distance_nm"] - PASS_RANGE_NM) < 0.5
    assert position["altitude_feet"] == START_FEET


def test_position_is_derived_from_elapsed_time(app):
    service  = ConcordeService("concorde", app.service("concorde"), app)
    at_spawn = service.refresh()["position"]

    _backdate(service, minutes=10)
    overhead = service.refresh()["position"]

    # 300kt for 10 minutes is 50nm — she should be over the observer, at cruise.
    assert overhead["distance_nm"] < 1.0
    assert overhead["altitude_feet"] == CRUISE_FEET
    assert at_spawn["distance_nm"] > overhead["distance_nm"]


def test_she_climbs_inbound_and_descends_outbound(app):
    service = ConcordeService("concorde", app.service("concorde"), app)
    service.refresh()

    _backdate(service, minutes=5)     # 25nm along a 100nm pass
    inbound = service.refresh()["position"]
    _backdate(service, minutes=15)    # 75nm along it
    outbound = service.refresh()["position"]

    assert inbound["vertical_rate_fpm"] > 0
    assert outbound["vertical_rate_fpm"] < 0
    assert START_FEET < inbound["altitude_feet"] < CRUISE_FEET
    assert START_FEET < outbound["altitude_feet"] < CRUISE_FEET


def test_the_pass_runs_through_the_observer(app):
    """Spawn is PASS_RANGE_NM out; the published pass says where."""
    flight = ConcordeService("concorde", app.service("concorde"), app).refresh()["pass"]

    spawn_distance = distance_nm(*OBSERVER, flight["spawn_lat"], flight["spawn_lon"])
    assert abs(spawn_distance - PASS_RANGE_NM) < 0.5


def test_the_state_file_is_plain_json(app, concorde):
    """Anything can read it — it is the service's whole interface."""
    loaded = json.loads(concorde.state_path.read_text())

    assert set(loaded) == {"observed_at", "position", "pass"}
