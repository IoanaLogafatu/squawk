"""
modules/ground_distance_filter.py

Processor filter module that filters aircraft based on ground distance (great-circle 2D distance),
supporting configurable distance bounds (min_distance and max_distance) and distance units
('miles', 'km', 'nm').
"""

from __future__ import annotations

import math
from typing import Optional

from modules import BaseModule, ModuleContext
from schemas.aircraft import Aircraft

# Unit conversion factors to Nautical Miles (nm)
# 1 nm = 1852 meters exactly
# 1 km = 1000 meters -> 1000 / 1852 nm
# 1 statute mile = 1609.344 meters -> 1609.344 / 1852 nm
_KM_TO_NM = 1000.0 / 1852.0
_MILE_TO_NM = 1609.344 / 1852.0

_UNIT_TO_NM_FACTOR = {
    "nm": 1.0,
    "nmi": 1.0,
    "nautical_miles": 1.0,
    "nautical_mile": 1.0,
    "km": _KM_TO_NM,
    "kilometers": _KM_TO_NM,
    "kilometer": _KM_TO_NM,
    "mi": _MILE_TO_NM,
    "mile": _MILE_TO_NM,
    "miles": _MILE_TO_NM,
    "statute_miles": _MILE_TO_NM,
}

EARTH_RADIUS_NM = 3440.065  # Mean radius of Earth in nautical miles


def haversine_distance_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate Haversine great-circle (ground) distance in nautical miles between two lat/lon points."""
    lat1_r, lon1_r = math.radians(lat1), math.radians(lon1)
    lat2_r, lon2_r = math.radians(lat2), math.radians(lon2)
    dlat = lat2_r - lat1_r
    dlon = lon2_r - lon1_r
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2) ** 2
    return 2 * math.asin(math.sqrt(a)) * EARTH_RADIUS_NM


class GroundDistanceFilter(BaseModule):

    def __init__(
        self,
        max_distance: Optional[float] = None,
        min_distance: Optional[float] = None,
        unit: str = "nm",
        observer_lat: Optional[float] = None,
        observer_lon: Optional[float] = None,
    ) -> None:
        unit_clean = str(unit).strip().lower()
        if unit_clean not in _UNIT_TO_NM_FACTOR:
            raise ValueError(
                f"ground_distance_filter: unsupported unit {unit!r}. "
                f"Must be one of: 'miles', 'km', 'nm'"
            )
        self._unit = unit_clean
        self._factor = _UNIT_TO_NM_FACTOR[unit_clean]

        self._max_distance_nm = float(max_distance) * self._factor if max_distance is not None else None
        self._min_distance_nm = float(min_distance) * self._factor if min_distance is not None else None
        self._observer_lat = float(observer_lat) if observer_lat is not None else None
        self._observer_lon = float(observer_lon) if observer_lon is not None else None

        if (
            self._min_distance_nm is not None
            and self._max_distance_nm is not None
            and self._min_distance_nm > self._max_distance_nm
        ):
            raise ValueError(
                f"ground_distance_filter: 'min_distance' ({min_distance}) cannot be greater than 'max_distance' ({max_distance})"
            )

    def _get_ground_distance_nm(self, a: Aircraft) -> Optional[float]:
        if a.location.distance_nm is not None:
            return float(a.location.distance_nm)

        if (
            self._observer_lat is not None
            and self._observer_lon is not None
            and a.location.latitude is not None
            and a.location.longitude is not None
        ):
            return haversine_distance_nm(
                self._observer_lat, self._observer_lon, float(a.location.latitude), float(a.location.longitude)
            )

        return None

    def process(self, aircraft: list[Aircraft]) -> list[Aircraft]:
        filtered = []
        for a in aircraft:
            dist_nm = self._get_ground_distance_nm(a)
            if dist_nm is None:
                continue
            if self._min_distance_nm is not None and dist_nm < self._min_distance_nm:
                continue
            if self._max_distance_nm is not None and dist_nm > self._max_distance_nm:
                continue
            filtered.append(a)
        return filtered


KEYS = {"max_distance", "min_distance", "unit", "observer_lat", "observer_lon"}


def get(cfg: dict, ctx: ModuleContext) -> GroundDistanceFilter:
    max_dist = cfg.get("max_distance")
    min_dist = cfg.get("min_distance")
    unit     = cfg.get("unit", "nm")

    obs_lat = cfg.get("observer_lat", ctx.observer.latitude)
    obs_lon = cfg.get("observer_lon", ctx.observer.longitude)

    return GroundDistanceFilter(
        max_distance=max_dist,
        min_distance=min_dist,
        unit=unit,
        observer_lat=obs_lat,
        observer_lon=obs_lon,
    )
