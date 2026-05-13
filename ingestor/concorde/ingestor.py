"""
squawk/ingestor/concorde/ingestor.py

Concorde dummy ingestor.

Simulates G-BOAC flying a straight pass over the observer's location.
Concorde spawns 50nm out on a randomly chosen cardinal bearing, flies
overhead at 300 knots and 5,000ft, continues 50nm the other side, then
resets on a new random bearing.

State is persisted to disk so the flight survives restarts — the demo
experience is watching Concorde approach, not seeing her randomly appear.

State file: <data_dir>/ingestors/concorde/concorde_state.json
"""

from __future__ import annotations

import json
import math
import random
import time
from datetime import datetime, timezone
from pathlib import Path

from config import config
from schemas.aircraft import (
    UNKNOWN,
    Aircraft,
    AircraftLocation,
    AircraftMeta,
    AircraftRaw,
    AircraftRoute,
    AircraftVector,
    Airframe,
    ReceiverStatus,
    SquawkEnvelope,
)

# ---------------------------------------------------------------------------
# Concorde constants
# ---------------------------------------------------------------------------

SOURCE_NAME     = "Concorde"
ICAO_HEX        = "400F6A"
REGISTRATION    = "G-BOAC"
OPERATOR        = "British Airways"
AIRCRAFT_TYPE   = "Concorde"
CALLSIGN        = "SPEEDBIRD002"
SPEED_KNOTS     = 300.0
ALTITUDE_FEET   = 5000
PASS_RANGE_NM   = 50.0          # Distance from observer to spawn/despawn
POLL_INTERVAL   = 5             # Seconds between position updates

# Cardinal bearings Concorde may fly (degrees true, direction of travel)
CARDINAL_BEARINGS = [0.0, 90.0, 180.0, 270.0]

# Earth radius in nautical miles
EARTH_RADIUS_NM = 3440.065


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _destination(lat: float, lon: float, bearing_deg: float, distance_nm: float) -> tuple[float, float]:
    """
    Calculate destination lat/lon given a start point, bearing, and distance.
    Uses the spherical Earth approximation — accurate enough for 100nm.
    """
    lat_r = math.radians(lat)
    lon_r = math.radians(lon)
    bearing_r = math.radians(bearing_deg)
    d = distance_nm / EARTH_RADIUS_NM

    dest_lat_r = math.asin(
        math.sin(lat_r) * math.cos(d)
        + math.cos(lat_r) * math.sin(d) * math.cos(bearing_r)
    )
    dest_lon_r = lon_r + math.atan2(
        math.sin(bearing_r) * math.sin(d) * math.cos(lat_r),
        math.cos(d) - math.sin(lat_r) * math.sin(dest_lat_r),
    )
    return math.degrees(dest_lat_r), math.degrees(dest_lon_r)


def _distance_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance in nautical miles between two points."""
    lat1_r, lon1_r = math.radians(lat1), math.radians(lon1)
    lat2_r, lon2_r = math.radians(lat2), math.radians(lon2)
    dlat = lat2_r - lat1_r
    dlon = lon2_r - lon1_r
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2) ** 2
    return 2 * math.asin(math.sqrt(a)) * EARTH_RADIUS_NM


def _bearing_to(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """True bearing from point 1 to point 2, in degrees."""
    lat1_r, lon1_r = math.radians(lat1), math.radians(lon1)
    lat2_r, lon2_r = math.radians(lat2), math.radians(lon2)
    dlon = lon2_r - lon1_r
    x = math.sin(dlon) * math.cos(lat2_r)
    y = math.cos(lat1_r) * math.sin(lat2_r) - math.sin(lat1_r) * math.cos(lat2_r) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def _state_path() -> Path:
    return Path(config.squawk.data_dir) / "ingestors" / "concorde" / "concorde_state.json"


def _load_state() -> dict | None:
    path = _state_path()
    if not path.exists():
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _save_state(state: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    tmp.replace(path)


def _new_pass(observer_lat: float, observer_lon: float) -> dict:
    """
    Start a new pass on a random cardinal bearing.
    Concorde spawns 50nm from the observer on the opposite bearing
    (so she flies toward and then past the observer).
    """
    travel_bearing = random.choice(CARDINAL_BEARINGS)
    spawn_bearing  = (travel_bearing + 180) % 360   # Spawn on the opposite side

    spawn_lat, spawn_lon = _destination(
        observer_lat, observer_lon, spawn_bearing, PASS_RANGE_NM
    )

    return {
        "travel_bearing": travel_bearing,
        "spawn_lat":       spawn_lat,
        "spawn_lon":       spawn_lon,
        "start_time":      datetime.now(timezone.utc).isoformat(),
        "speed_knots":     SPEED_KNOTS,
        "altitude_feet":   ALTITUDE_FEET,
    }


def _current_position(state: dict) -> tuple[float, float]:
    """
    Calculate Concorde's current position from state.
    Position is derived from elapsed time and speed — no need to store it.
    """
    start = datetime.fromisoformat(state["start_time"])
    elapsed_hours = (datetime.now(timezone.utc) - start).total_seconds() / 3600
    distance_nm   = state["speed_knots"] * elapsed_hours

    lat, lon = _destination(
        state["spawn_lat"],
        state["spawn_lon"],
        state["travel_bearing"],
        distance_nm,
    )
    return lat, lon


def _pass_complete(state: dict, observer_lat: float, observer_lon: float) -> bool:
    """
    Pass is complete when Concorde is 50nm past the observer on her travel bearing.
    Total pass length is 100nm.
    """
    start = datetime.fromisoformat(state["start_time"])
    elapsed_hours  = (datetime.now(timezone.utc) - start).total_seconds() / 3600
    distance_nm    = state["speed_knots"] * elapsed_hours
    return distance_nm >= PASS_RANGE_NM * 2


# ---------------------------------------------------------------------------
# Build envelope
# ---------------------------------------------------------------------------

def _build_envelope(lat: float, lon: float, distance_nm: float, track: float) -> SquawkEnvelope:

    meta = AircraftMeta(
        icao_hex       = ICAO_HEX,
        ingestor       = "concorde",
        observed_at    = datetime.now(timezone.utc),
        reception_type = "adsb_icao",
    )

    location = AircraftLocation(
        latitude        = round(lat, 6),
        longitude       = round(lon, 6),
        altitude_feet   = ALTITUDE_FEET,
        distance_nm     = round(distance_nm, 3),
        seen_seconds    = 0.0,
    )

    direction = AircraftVector(
        ground_speed_knots = SPEED_KNOTS,
        track_degrees      = round(track, 2),
        vertical_rate_fpm  = 0,
    )

    route = AircraftRoute(
        callsign         = CALLSIGN,
        squawk_code      = "2346",
        origin_iata      = UNKNOWN,
        destination_iata = UNKNOWN,
        flight_number    = UNKNOWN,
    )

    airframe = Airframe(
        registration  = REGISTRATION,
        aircraft_type = AIRCRAFT_TYPE,
        operator      = OPERATOR,
    )

    aircraft = Aircraft(
        meta      = meta,
        location  = location,
        direction = direction,
        route     = route,
        airframe  = airframe,
        raw       = AircraftRaw(payload={}),
    )

    return SquawkEnvelope(
        source          = SOURCE_NAME,
        timestamp       = datetime.now(timezone.utc),
        aircraft_count  = 1,
        receiver_status = [ReceiverStatus(
            name      = "concorde",
            healthy   = True,
            last_seen = datetime.now(timezone.utc),
            error     = UNKNOWN,
        )],
        aircraft        = [aircraft],
    )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run() -> None:
    cfg = config.ingestors.get("concorde", {})

    if not cfg.get("enabled", False):
        print(f"{SOURCE_NAME}: disabled in config, exiting.")
        return

    observer_lat = config.observer.latitude
    observer_lon = config.observer.longitude

    from storage import get_storage
    storage = get_storage(config.storage.method, config.squawk.data_dir)

    # Resume existing pass or start a new one
    state = _load_state()
    if state is None:
        state = _new_pass(observer_lat, observer_lon)
        _save_state(state)

    while True:
        cycle_start = time.time()

        # Check if this pass is complete — start a new one if so
        if _pass_complete(state, observer_lat, observer_lon):
            state = _new_pass(observer_lat, observer_lon)
            _save_state(state)

        lat, lon = _current_position(state)
        distance  = _distance_nm(observer_lat, observer_lon, lat, lon)
        track     = state["travel_bearing"]

        envelope = _build_envelope(lat, lon, distance, track)
        storage.save_aircraft_array(envelope.aircraft)

        sleep_for = max(0.0, POLL_INTERVAL - (time.time() - cycle_start))
        time.sleep(sleep_for)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print("\nStopped.")
