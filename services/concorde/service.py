"""
services/concorde/service.py

The Concorde simulator — an always-on service process.

    python main.py service concorde

G-BOAC flies straight passes over the observer: she spawns 50nm out on a
randomly chosen cardinal bearing, climbs on the way in, crosses overhead,
descends on the way out, and 50nm past the observer the pass ends and a
new one begins on a fresh bearing.

About once a second the service works out where she is and publishes it
to the state file. The ingest chains that report her only ever read that
file, so every one of them sees the same flight however many there are,
and none of them has to agree with the others about anything.

The pass parameters — bearing, spawn point, start time — are published
alongside the position, and the position is derived from how long ago the
pass started. A restarted service picks those parameters back up and
resumes the pass in progress rather than teleporting Concorde back to the
spawn point. The demo is watching her approach.

State file: <data_dir>/services/concorde/state.json

    {
      "observed_at": ISO 8601 UTC — when this position was computed,
      "position":    latitude, longitude, altitude_feet, ...,
      "pass":        the parameters of the pass in progress
    }
"""

from __future__ import annotations

import math
import random
from datetime import datetime
from typing import TYPE_CHECKING, Any

from schemas.aircraft import ensure_utc, now_utc
from services.base import BaseService, read_state

if TYPE_CHECKING:
    from config import Config, ServiceConfig

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
        name:    the service's config name — "concorde".
        service: its settings; Concorde has none beyond the common ones.
        app:     installation config; passes are drawn around its observer.
    """

    refresh_interval_seconds = 1.0

    def __init__(self, name: str, service: "ServiceConfig", app: "Config") -> None:
        super().__init__(name, service, app)
        self.observer_lat = app.observer_latitude
        self.observer_lon = app.observer_longitude
        self.flight: dict[str, Any] | None = self._resume()

    # -- the work -----------------------------------------------------------

    def refresh(self) -> dict[str, Any]:
        """Where she is now, starting a new pass if there is none to continue."""
        now = now_utc()

        if self.flight is None:
            self.flight = self._new_pass(now)
            self.log.info("new pass on bearing %.0f", self.flight["travel_bearing"])
        elif self._flown_nm(self.flight, now) >= PASS_RANGE_NM * 2:
            self.flight = self._new_pass(now)
            self.log.info("previous pass complete — new pass on bearing %.0f",
                          self.flight["travel_bearing"])

        return {
            "observed_at": now.isoformat(),
            "position":    self._derive(self.flight, now),
            "pass":        self.flight,
        }

    # -- pass lifecycle -----------------------------------------------------

    def _resume(self) -> dict[str, Any] | None:
        """
        The pass a previous run of this service left in progress, if any.

        Not resumed if it is unusable, already finished, or was drawn around
        a different observer — a pass around the old position would never
        come overhead.
        """
        previous = read_state(self.app.data_dir, self.name) or {}
        flight   = previous.get("pass")
        if not isinstance(flight, dict) or not self._usable(flight):
            return None
        if (flight["observer_lat"], flight["observer_lon"]) != (self.observer_lat, self.observer_lon):
            return None
        try:
            if self._flown_nm(flight, now_utc()) >= PASS_RANGE_NM * 2:
                return None
        except (TypeError, ValueError):
            return None
        self.log.info("resuming the pass in progress on bearing %.0f", flight["travel_bearing"])
        return flight

    def _new_pass(self, now: datetime) -> dict[str, Any]:
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
            "start_time":     now.isoformat(),
            "speed_knots":    SPEED_KNOTS,
            "observer_lat":   self.observer_lat,
            "observer_lon":   self.observer_lon,
        }

    @staticmethod
    def _usable(flight: dict[str, Any]) -> bool:
        """Pass parameters missing any field are treated as no pass at all."""
        required = ("travel_bearing", "spawn_lat", "spawn_lon", "start_time",
                    "speed_knots", "observer_lat", "observer_lon")
        return all(key in flight for key in required)

    # -- derivation ---------------------------------------------------------

    @staticmethod
    def _flown_nm(flight: dict[str, Any], now: datetime) -> float:
        """Distance flown since the pass started."""
        start = ensure_utc(datetime.fromisoformat(flight["start_time"]))
        hours = (now - start).total_seconds() / 3600
        return max(0.0, flight["speed_knots"] * hours)

    def _derive(self, flight: dict[str, Any], now: datetime) -> dict[str, Any]:
        flown = min(self._flown_nm(flight, now), PASS_RANGE_NM * 2)
        total = PASS_RANGE_NM * 2

        lat, lon = destination(
            flight["spawn_lat"], flight["spawn_lon"], flight["travel_bearing"], flown
        )
        altitude, vertical_rate = self._altitude_profile(flown, flight["speed_knots"])

        return {
            "latitude":           round(lat, 6),
            "longitude":          round(lon, 6),
            "altitude_feet":      altitude,
            "ground_speed_knots": flight["speed_knots"],
            "heading":            flight["travel_bearing"],
            "vertical_rate_fpm":  vertical_rate,
            "distance_nm":        round(
                distance_nm(self.observer_lat, self.observer_lon, lat, lon), 3),
            "bearing_degrees":    round(
                bearing_to(self.observer_lat, self.observer_lon, lat, lon), 2),
            "elapsed_seconds":    round(flown / flight["speed_knots"] * 3600, 1),
            "pass_fraction":      round(flown / total, 4),
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
