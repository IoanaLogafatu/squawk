"""
services/concorde/service.py

The Concorde simulator — a shared service, not a running process.

G-BOAC flies straight passes over the observer: she spawns 50nm out on a
randomly chosen cardinal bearing, climbs on the way in, crosses overhead,
descends on the way out, and 50nm past the observer the pass ends and a
new one begins on a fresh bearing.

Nothing here runs a loop or holds a position. get_position() is a pure
function of (state file, clock): only the pass *parameters* are stored —
bearing, spawn point, start time — and the position is derived from how
long ago the pass started. Two consequences, both wanted:

  - Every process that asks at the same moment gets the identical answer,
    with no coordination beyond the file.
  - A restart resumes the pass in progress rather than teleporting
    Concorde back to the spawn point. The demo is watching her approach.

The lock earns its keep at exactly one moment: the handover between
passes. Two ingest processes asking within milliseconds of each other must
not both conclude "no pass in progress" and start two different flights.
Whoever takes the lock first either finds a live pass or starts one; the
second takes the lock afterwards and sees that decision.

State file: <data_dir>/services/concorde/state.json
"""

from __future__ import annotations

import math
import random
from datetime import datetime
from pathlib import Path
from typing import Any

from schemas.aircraft import ensure_utc, now_utc
from services.base import BaseService

# ---------------------------------------------------------------------------
# Flight profile
# ---------------------------------------------------------------------------

PASS_RANGE_NM     = 50.0    # Spawn distance, and despawn distance the far side
SPEED_KNOTS       = 300.0   # Ground speed, constant across the pass
CRUISE_FEET       = 12000   # Altitude directly over the observer
START_FEET        = 2000    # Altitude at spawn, and again at despawn

# Cardinal bearings she may fly (degrees true, direction of travel)
CARDINAL_BEARINGS = [0.0, 90.0, 180.0, 270.0]

EARTH_RADIUS_NM = 3440.065


# ---------------------------------------------------------------------------
# Geometry — spherical Earth, plenty accurate over a 100nm pass
# ---------------------------------------------------------------------------

def destination(lat: float, lon: float, bearing_deg: float, distance_nm: float) -> tuple[float, float]:
    """Point reached by travelling distance_nm from (lat, lon) on a bearing."""
    lat_r     = math.radians(lat)
    lon_r     = math.radians(lon)
    bearing_r = math.radians(bearing_deg)
    d         = distance_nm / EARTH_RADIUS_NM

    dest_lat_r = math.asin(
        math.sin(lat_r) * math.cos(d)
        + math.cos(lat_r) * math.sin(d) * math.cos(bearing_r)
    )
    dest_lon_r = lon_r + math.atan2(
        math.sin(bearing_r) * math.sin(d) * math.cos(lat_r),
        math.cos(d) - math.sin(lat_r) * math.sin(dest_lat_r),
    )
    return math.degrees(dest_lat_r), math.degrees(dest_lon_r)


def distance_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance in nautical miles."""
    lat1_r, lon1_r = math.radians(lat1), math.radians(lon1)
    lat2_r, lon2_r = math.radians(lat2), math.radians(lon2)
    dlat = lat2_r - lat1_r
    dlon = lon2_r - lon1_r
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2) ** 2
    return 2 * math.asin(math.sqrt(a)) * EARTH_RADIUS_NM


def bearing_to(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """True bearing from point 1 to point 2, 0-359 degrees."""
    lat1_r, lon1_r = math.radians(lat1), math.radians(lon1)
    lat2_r, lon2_r = math.radians(lat2), math.radians(lon2)
    dlon = lon2_r - lon1_r
    x = math.sin(dlon) * math.cos(lat2_r)
    y = math.cos(lat1_r) * math.sin(lat2_r) - math.sin(lat1_r) * math.cos(lat2_r) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class ConcordeService(BaseService):
    """
    Args:
        data_dir:      installation data directory.
        observer_lat:  observer latitude — passes are drawn around it.
        observer_lon:  observer longitude.
    """

    def __init__(self, data_dir: Path, observer_lat: float, observer_lon: float) -> None:
        super().__init__("concorde", data_dir)
        self.observer_lat = observer_lat
        self.observer_lon = observer_lon

    # -- public -------------------------------------------------------------

    def get_position(self) -> dict[str, Any]:
        """
        Concorde's position right now.

        Starts a new pass if there is no state, if the stored state is
        unusable, or if the pass in progress has finished. The whole
        check-and-maybe-start sequence is under the lock — see the module
        docstring for why that specifically matters.

        Returns a dict of latitude, longitude, altitude_feet,
        ground_speed_knots, track_degrees, vertical_rate_fpm, distance_nm,
        bearing_degrees and elapsed_seconds.
        """
        with self.locked():
            state = self.read_state()

            if state is None or not self._usable(state):
                state = self._new_pass()
                self.write_state(state)
                self.log.debug("started a new pass on bearing %.0f", state["travel_bearing"])
            elif self._elapsed_nm(state) >= PASS_RANGE_NM * 2:
                state = self._new_pass()
                self.write_state(state)
                self.log.debug("previous pass complete — new pass on bearing %.0f",
                               state["travel_bearing"])

        # Derivation needs no lock: state is immutable for the life of a pass.
        return self._derive(state)

    # -- pass lifecycle -----------------------------------------------------

    def _new_pass(self) -> dict[str, Any]:
        """
        Pass parameters for a fresh flight.

        She spawns on the bearing opposite the one she will travel, so the
        pass runs inbound to the observer and then out the other side.
        """
        travel_bearing = random.choice(CARDINAL_BEARINGS)
        spawn_bearing  = (travel_bearing + 180) % 360

        spawn_lat, spawn_lon = destination(
            self.observer_lat, self.observer_lon, spawn_bearing, PASS_RANGE_NM
        )

        return {
            "travel_bearing": travel_bearing,
            "spawn_lat":      spawn_lat,
            "spawn_lon":      spawn_lon,
            "start_time":     now_utc().isoformat(),
            "speed_knots":    SPEED_KNOTS,
        }

    @staticmethod
    def _usable(state: dict[str, Any]) -> bool:
        """A state file missing any pass parameter is treated as no state at all."""
        required = ("travel_bearing", "spawn_lat", "spawn_lon", "start_time", "speed_knots")
        return all(key in state for key in required)

    # -- derivation ---------------------------------------------------------

    def _elapsed_nm(self, state: dict[str, Any]) -> float:
        """Distance flown since the pass started."""
        start   = ensure_utc(datetime.fromisoformat(state["start_time"]))
        hours   = (now_utc() - start).total_seconds() / 3600
        return max(0.0, state["speed_knots"] * hours)

    def _derive(self, state: dict[str, Any]) -> dict[str, Any]:
        flown = self._elapsed_nm(state)
        total = PASS_RANGE_NM * 2

        lat, lon = destination(
            state["spawn_lat"], state["spawn_lon"], state["travel_bearing"], flown
        )
        altitude, vertical_rate = self._altitude_profile(flown, state["speed_knots"])

        return {
            "latitude":           round(lat, 6),
            "longitude":          round(lon, 6),
            "altitude_feet":      altitude,
            "ground_speed_knots": state["speed_knots"],
            "track_degrees":      state["travel_bearing"],
            "vertical_rate_fpm":  vertical_rate,
            "distance_nm":        round(
                distance_nm(self.observer_lat, self.observer_lon, lat, lon), 3),
            "bearing_degrees":    round(
                bearing_to(self.observer_lat, self.observer_lon, lat, lon), 2),
            "elapsed_seconds":    round(flown / state["speed_knots"] * 3600, 1),
            "pass_fraction":      round(min(1.0, flown / total), 4),
        }

    @staticmethod
    def _altitude_profile(flown_nm: float, speed_knots: float) -> tuple[int, int]:
        """
        Altitude and vertical rate at a point in the pass.

        Profile chosen: a linear climb from START_FEET at spawn to
        CRUISE_FEET directly overhead, then a mirrored descent back to
        START_FEET at despawn. Linear in distance, and speed is constant,
        so the vertical rate is a constant +/- value either side of the
        midpoint — easy to eyeball on the console output, which is the
        point of a simulator in a base build.
        """
        total = PASS_RANGE_NM * 2
        flown = min(max(flown_nm, 0.0), total)
        half  = total / 2

        climbing = flown <= half
        progress = (flown / half) if climbing else ((total - flown) / half)
        altitude = START_FEET + (CRUISE_FEET - START_FEET) * progress

        # Feet gained per nm, converted to feet per minute at this speed.
        feet_per_nm  = (CRUISE_FEET - START_FEET) / half
        nm_per_minute = speed_knots / 60
        rate = feet_per_nm * nm_per_minute
        vertical_rate = int(round(rate if climbing else -rate))

        return int(round(altitude)), vertical_rate
