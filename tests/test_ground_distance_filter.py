"""
tests/test_ground_distance_filter.py

Tests for the ground_distance_filter module.
"""

from __future__ import annotations

import pytest

from modules.ground_distance_filter import GroundDistanceFilter, get, haversine_distance_nm
from schemas.aircraft import (
    Aircraft, AircraftLocation, AircraftMeta, AircraftRaw,
    AircraftRoute, AircraftVector, Airframe,
)


def _make_aircraft(
    hex_id: str = "400A0A",
    distance_nm: float | None = None,
    lat: float | None = None,
    lon: float | None = None,
) -> Aircraft:
    return Aircraft(
        meta      = AircraftMeta(icao_hex=hex_id, ingestor="test", reception_type="adsb_icao"),
        location  = AircraftLocation(distance_nm=distance_nm, latitude=lat, longitude=lon),
        direction = AircraftVector(),
        route     = AircraftRoute(),
        airframe  = Airframe(),
        raw       = AircraftRaw(),
    )


def test_ground_distance_filter_units():
    # 25 statute miles in nm is ~21.72 nm
    # 10 nm aircraft -> within 25 miles (keep)
    # 23 nm aircraft -> ~26.47 miles, outside 25 miles (filter)
    rf_miles = GroundDistanceFilter(max_distance=25, unit="miles")
    a_close = _make_aircraft(hex_id="A1", distance_nm=10.0)
    a_far   = _make_aircraft(hex_id="A2", distance_nm=23.0)

    res = rf_miles.process([a_close, a_far])
    assert res == [a_close]

    # 50 km in nm is ~26.99 nm
    rf_km = GroundDistanceFilter(max_distance=50, unit="km")
    assert rf_km.process([a_close, a_far]) == [a_close, a_far]

    rf_nm = GroundDistanceFilter(max_distance=15, unit="nm")
    assert rf_nm.process([a_close, a_far]) == [a_close]


def test_ground_distance_filter_modes():
    # Mode 1: max_distance only (less than)
    rf_less = GroundDistanceFilter(max_distance=20, unit="nm")
    a5 = _make_aircraft(hex_id="5", distance_nm=5.0)
    a15 = _make_aircraft(hex_id="15", distance_nm=15.0)
    a25 = _make_aircraft(hex_id="25", distance_nm=25.0)

    assert rf_less.process([a5, a15, a25]) == [a5, a15]

    # Mode 2: min_distance only (greater than)
    rf_greater = GroundDistanceFilter(min_distance=10, unit="nm")
    assert rf_greater.process([a5, a15, a25]) == [a15, a25]

    # Mode 3: Range (both min and max)
    rf_range = GroundDistanceFilter(min_distance=10, max_distance=20, unit="nm")
    assert rf_range.process([a5, a15, a25]) == [a15]


def test_ground_distance_filter_invalid_bounds():
    # min_distance > max_distance must raise ValueError
    with pytest.raises(ValueError, match="cannot be greater than"):
        GroundDistanceFilter(min_distance=25, max_distance=10, unit="nm")


def test_ground_distance_filter_invalid_unit():
    with pytest.raises(ValueError, match="unsupported unit"):
        GroundDistanceFilter(max_distance=10, unit="parsecs")


def test_haversine_fallback():
    # Observer at (53.7778, -1.5721)
    obs_lat, obs_lon = 53.7778, -1.5721
    rf = GroundDistanceFilter(max_distance=25, unit="miles", observer_lat=obs_lat, observer_lon=obs_lon)

    # Aircraft directly overhead (distance = 0 miles) without distance_nm
    a_overhead = _make_aircraft(hex_id="OVERHEAD", distance_nm=None, lat=obs_lat, lon=obs_lon)

    # Aircraft ~50 miles away without distance_nm
    a_far_latlon = _make_aircraft(hex_id="FAR", distance_nm=None, lat=54.5, lon=-1.5721)

    # Aircraft with no distance and no lat/lon
    a_nodata = _make_aircraft(hex_id="NODATA", distance_nm=None, lat=None, lon=None)

    result = rf.process([a_overhead, a_far_latlon, a_nodata])
    assert result == [a_overhead]


def test_factory_get_aliases():
    # Test 'distance', 'within', 'below' config keys
    rf1 = get({"distance": 25, "unit": "miles"})
    assert rf1._max_distance_nm == pytest.approx(25 * (1609.344 / 1852.0))

    rf2 = get({"within": 10, "above": 2, "unit": "km"})
    assert rf2._max_distance_nm == pytest.approx(10 * (1000.0 / 1852.0))
    assert rf2._min_distance_nm == pytest.approx(2 * (1000.0 / 1852.0))

    rf3 = get({"below": 30, "unit": "nm"})
    assert rf3._max_distance_nm == pytest.approx(30.0)
