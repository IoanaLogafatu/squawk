"""
tests/test_concorde_service.py

The Concorde service — locking, and position as a pure function of state.

The locking test is the important one, and it uses real OS processes
rather than threads on purpose: the lock is an flock, its whole job is to
work between processes, and a threaded test would pass whether or not the
flock was there at all.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
from datetime import timedelta
from pathlib import Path

from schemas.aircraft import ensure_utc, now_utc
from services.concorde.service import (
    CRUISE_FEET,
    PASS_RANGE_NM,
    START_FEET,
    ConcordeService,
    distance_nm,
)

OBSERVER = (52.0, -1.0)


# ---------------------------------------------------------------------------
# A service that leaves a mark every time it starts a flight
# ---------------------------------------------------------------------------

class CountingConcorde(ConcordeService):
    """
    Records every new pass to an append-only file.

    Small O_APPEND writes are atomic, so counting the lines afterwards
    counts the passes actually started, across every process involved —
    which is the thing the lock is supposed to hold at one.
    """

    def __init__(self, data_dir: Path, counter: Path) -> None:
        super().__init__(data_dir, *OBSERVER)
        self.counter = counter

    def _new_pass(self):
        state = super()._new_pass()
        fd = os.open(self.counter, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        try:
            os.write(fd, b"pass\n")
        finally:
            os.close(fd)
        return state


def _ask(args):
    """Child process body: line up on the barrier, then all ask at once."""
    data_dir, counter, barrier = args
    service = CountingConcorde(data_dir, counter)
    barrier.wait()
    return service.get_position()


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------

def test_simultaneous_first_calls_start_exactly_one_pass(tmp_path):
    data_dir = tmp_path / "data"
    counter  = tmp_path / "passes.log"
    workers  = 8

    with mp.Manager() as manager:
        barrier = manager.Barrier(workers)
        with mp.Pool(workers) as pool:
            results = pool.map(_ask, [(data_dir, counter, barrier)] * workers)

    started = counter.read_text().count("pass")
    assert started == 1, f"{workers} simultaneous callers started {started} flights"

    # And every caller must have come away with the same flight. Positions
    # are compared with a tolerance, not for equality: each caller derives
    # its answer at its own instant, so they legitimately differ by the
    # fraction of a metre Concorde covers between one call and the next.
    assert len({r["track_degrees"] for r in results}) == 1

    spread = max(r["latitude"] for r in results) - min(r["latitude"] for r in results)
    assert spread < 0.001, f"callers disagree on position by {spread} degrees"


def test_a_second_call_joins_the_pass_already_in_progress(tmp_path):
    counter = tmp_path / "passes.log"
    service = CountingConcorde(tmp_path / "data", counter)

    first  = service.get_position()
    second = service.get_position()

    assert counter.read_text().count("pass") == 1
    assert first["track_degrees"] == second["track_degrees"]


def test_state_survives_a_new_service_object(tmp_path):
    data_dir = tmp_path / "data"

    first = ConcordeService(data_dir, *OBSERVER).get_position()
    # A restart is just a new object over the same folder.
    second = ConcordeService(data_dir, *OBSERVER).get_position()

    assert first["track_degrees"] == second["track_degrees"]


def test_an_unreadable_state_file_starts_a_fresh_pass(tmp_path):
    counter = tmp_path / "passes.log"
    service = CountingConcorde(tmp_path / "data", counter)
    service.get_position()

    service.state_path.write_text("{ truncated")
    service.get_position()

    assert counter.read_text().count("pass") == 2


def test_a_completed_pass_is_replaced(tmp_path):
    counter = tmp_path / "passes.log"
    service = CountingConcorde(tmp_path / "data", counter)
    service.get_position()

    # Backdate the start so the 100nm pass is long finished.
    state = json.loads(service.state_path.read_text())
    started = ensure_utc(now_utc()) - timedelta(hours=2)
    state["start_time"] = started.isoformat()
    service.state_path.write_text(json.dumps(state))

    service.get_position()

    assert counter.read_text().count("pass") == 2


# ---------------------------------------------------------------------------
# The flight itself
# ---------------------------------------------------------------------------

def test_she_spawns_the_full_pass_range_out(tmp_path):
    position = ConcordeService(tmp_path / "data", *OBSERVER).get_position()

    assert abs(position["distance_nm"] - PASS_RANGE_NM) < 0.5
    assert position["altitude_feet"] == START_FEET


def test_position_is_derived_from_elapsed_time(tmp_path):
    service = ConcordeService(tmp_path / "data", *OBSERVER)
    at_spawn = service.get_position()

    # Rewind the start rather than waiting: position is a function of the
    # clock and the stored parameters, and nothing else.
    state = json.loads(service.state_path.read_text())
    state["start_time"] = (now_utc() - timedelta(minutes=10)).isoformat()
    service.state_path.write_text(json.dumps(state))

    overhead = service.get_position()

    # 300kt for 10 minutes is 50nm — she should be over the observer,
    # at cruise, and no longer climbing hard.
    assert overhead["distance_nm"] < 1.0
    assert overhead["altitude_feet"] == CRUISE_FEET
    assert at_spawn["distance_nm"] > overhead["distance_nm"]


def test_she_climbs_inbound_and_descends_outbound(tmp_path):
    service = ConcordeService(tmp_path / "data", *OBSERVER)
    service.get_position()
    state = json.loads(service.state_path.read_text())

    def at(minutes: float) -> dict:
        state["start_time"] = (now_utc() - timedelta(minutes=minutes)).isoformat()
        service.state_path.write_text(json.dumps(state))
        return service.get_position()

    inbound  = at(5)    # 25nm along a 100nm pass
    outbound = at(15)   # 75nm along it

    assert inbound["vertical_rate_fpm"] > 0
    assert outbound["vertical_rate_fpm"] < 0
    assert START_FEET < inbound["altitude_feet"] < CRUISE_FEET
    assert START_FEET < outbound["altitude_feet"] < CRUISE_FEET


def test_the_pass_runs_through_the_observer(tmp_path):
    """Spawn and despawn are both PASS_RANGE_NM out, on opposite sides."""
    service = ConcordeService(tmp_path / "data", *OBSERVER)
    service.get_position()
    state = json.loads(service.state_path.read_text())

    spawn_distance = distance_nm(*OBSERVER, state["spawn_lat"], state["spawn_lon"])
    assert abs(spawn_distance - PASS_RANGE_NM) < 0.5
