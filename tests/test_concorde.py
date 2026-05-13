"""
tests/test_concorde.py

pytest tests for the Concorde dummy ingestor.

Covers:
  1. Geometry helpers  (_destination, _distance_nm, _bearing_to)
  2. Pass completion   (_pass_complete)
  3. Position          (_current_position)
  4. Envelope contract (_build_envelope)
  5. New state         (_new_pass)

No file I/O, no network calls, no config dependency.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from ingestor.concorde.ingestor import (
    ALTITUDE_FEET,
    CARDINAL_BEARINGS,
    PASS_RANGE_NM,
    SPEED_KNOTS,
    _bearing_to,
    _build_envelope,
    _current_position,
    _destination,
    _distance_nm,
    _new_pass,
    _pass_complete,
)
from schemas.encoder import SquawkEncoder

OBS_LAT = 52.6376
OBS_LON = -1.1350


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _state(start_offset_seconds: float = 0.0) -> dict:
    """Minimal state dict for a north-bound pass spawning 50nm south of observer."""
    spawn_lat, spawn_lon = _destination(OBS_LAT, OBS_LON, 180.0, PASS_RANGE_NM)
    return {
        "travel_bearing": 0.0,
        "spawn_lat":       spawn_lat,
        "spawn_lon":       spawn_lon,
        "start_time":      (
            datetime.now(timezone.utc) - timedelta(seconds=start_offset_seconds)
        ).isoformat(),
        "speed_knots":     SPEED_KNOTS,
        "altitude_feet":   ALTITUDE_FEET,
    }


# ---------------------------------------------------------------------------
# Envelope fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def envelope():
    return _build_envelope(lat=OBS_LAT, lon=OBS_LON, distance_nm=0.0, track=0.0)


# ===========================================================================
# 1. Geometry tests
# ===========================================================================

def test_destination_north():
    lat, lon = _destination(OBS_LAT, OBS_LON, 0.0, 60.0)
    assert lat == pytest.approx(53.671, abs=0.1)
    assert lon == pytest.approx(OBS_LON, abs=0.1)


def test_distance_nm():
    lat, lon = _destination(OBS_LAT, OBS_LON, 0.0, 60.0)
    assert _distance_nm(OBS_LAT, OBS_LON, lat, lon) == pytest.approx(60.0, abs=0.1)


def test_bearing_to_north():
    north_lat, north_lon = _destination(OBS_LAT, OBS_LON, 0.0, 60.0)
    assert _bearing_to(OBS_LAT, OBS_LON, north_lat, north_lon) == pytest.approx(0.0, abs=0.1)


def test_bearing_to_east():
    east_lat, east_lon = _destination(OBS_LAT, OBS_LON, 90.0, 60.0)
    assert _bearing_to(OBS_LAT, OBS_LON, east_lat, east_lon) == pytest.approx(90.0, abs=0.1)


def test_destination_distance_consistency():
    dest_lat, dest_lon = _destination(OBS_LAT, OBS_LON, 45.0, 80.0)
    assert _distance_nm(OBS_LAT, OBS_LON, dest_lat, dest_lon) == pytest.approx(80.0, abs=0.1)


# ===========================================================================
# 2. Pass completion tests
# ===========================================================================

def test_pass_not_complete_at_start():
    assert _pass_complete(_state(0.0), OBS_LAT, OBS_LON) is False


def test_pass_complete_when_past_100nm():
    # 1 hour at 300 knots = 300nm, well past the 100nm threshold
    assert _pass_complete(_state(3600.0), OBS_LAT, OBS_LON) is True


def test_pass_complete_at_boundary():
    # Exactly 100nm at 300 knots = 1200s; any execution latency tips it past the boundary
    assert _pass_complete(_state(1200.0), OBS_LAT, OBS_LON) is True


# ===========================================================================
# 3. Position tests
# ===========================================================================

def test_position_at_start_is_near_spawn():
    state = _state(0.0)
    lat, lon = _current_position(state)
    assert lat == pytest.approx(state["spawn_lat"], abs=0.1)
    assert lon == pytest.approx(state["spawn_lon"], abs=0.1)


def test_position_at_50nm_is_near_observer():
    # 50nm at 300 knots = 600 seconds; spawn is 50nm south so Concorde is overhead
    state = _state(600.0)
    lat, lon = _current_position(state)
    assert lat == pytest.approx(OBS_LAT, abs=0.1)
    assert lon == pytest.approx(OBS_LON, abs=0.1)


def test_position_at_100nm_is_past_observer():
    # 100nm at 300 knots = 1200 seconds; 50nm north of observer on bearing 0
    state = _state(1200.0)
    lat, lon = _current_position(state)
    expected_lat, expected_lon = _destination(OBS_LAT, OBS_LON, 0.0, PASS_RANGE_NM)
    assert lat == pytest.approx(expected_lat, abs=0.1)
    assert lon == pytest.approx(expected_lon, abs=0.1)


# ===========================================================================
# 4. Envelope contract
# ===========================================================================

def test_envelope_meta(envelope):
    ac = envelope.aircraft[0]
    assert ac.meta.icao_hex == "400F6A"
    assert ac.meta.ingestor == "concorde"
    assert ac.meta.reception_type == "adsb_icao"


def test_envelope_route(envelope):
    assert envelope.aircraft[0].route.callsign == "SPEEDBIRD002"


def test_envelope_airframe(envelope):
    ac = envelope.aircraft[0]
    assert ac.airframe.registration == "G-BOAC"
    assert ac.airframe.operator == "British Airways"
    assert ac.airframe.aircraft_type == "Concorde"


def test_envelope_location_and_direction(envelope):
    loc = envelope.aircraft[0].location
    vec = envelope.aircraft[0].direction
    assert loc.altitude_feet == 5000
    assert loc.seen_seconds == 0.0
    assert vec.ground_speed_knots == 300.0
    assert vec.vertical_rate_fpm == 0


def test_envelope_source(envelope):
    assert envelope.source == "Concorde"


def test_envelope_aircraft_count(envelope):
    assert envelope.aircraft_count == 1


def test_envelope_timestamp_is_timezone_aware(envelope):
    assert isinstance(envelope.timestamp, datetime)
    assert envelope.timestamp.tzinfo is not None


def test_envelope_receiver_status(envelope):
    assert len(envelope.receiver_status) == 1
    rs = envelope.receiver_status[0]
    assert rs.name == "concorde"
    assert rs.healthy is True
    assert isinstance(rs.last_seen, datetime) and rs.last_seen.tzinfo is not None
    assert rs.error is None


def test_envelope_json_round_trip(envelope):
    serialised = json.dumps(envelope, cls=SquawkEncoder)
    data = json.loads(serialised)
    assert data["source"] == envelope.source
    assert data["aircraft_count"] == envelope.aircraft_count
    assert datetime.fromisoformat(data["timestamp"]) == envelope.timestamp
    assert isinstance(data["receiver_status"], list)


# ===========================================================================
# 5. New state test
# ===========================================================================

def test_new_pass_bearing_is_cardinal():
    state = _new_pass(OBS_LAT, OBS_LON)
    assert state["travel_bearing"] in CARDINAL_BEARINGS


def test_new_pass_spawn_distance():
    state = _new_pass(OBS_LAT, OBS_LON)
    dist = _distance_nm(OBS_LAT, OBS_LON, state["spawn_lat"], state["spawn_lon"])
    assert dist == pytest.approx(PASS_RANGE_NM, abs=0.1)


def test_new_pass_speed_and_altitude():
    state = _new_pass(OBS_LAT, OBS_LON)
    assert state["speed_knots"] == SPEED_KNOTS
    assert state["altitude_feet"] == ALTITUDE_FEET


def test_new_pass_start_time_is_valid_iso():
    state = _new_pass(OBS_LAT, OBS_LON)
    dt = datetime.fromisoformat(state["start_time"])
    assert dt.tzinfo is not None
