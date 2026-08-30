"""
tests/test_altitude_filter.py

Tests for the altitude_filter module.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from config import ObserverConfig
from modules import ModuleContext
from modules.altitude_filter import AltitudeFilter, get
from schemas.aircraft import (
    Aircraft, AircraftLocation, AircraftMeta, AircraftRaw,
    AircraftRoute, AircraftVector, Airframe,
)

# altitude_filter ignores ctx entirely — any placeholder will do.
_CTX = ModuleContext(
    data_dir=Path("."),
    module_dir=Path("./modules/altitude_filter"),
    observer=ObserverConfig(latitude=0.0, longitude=0.0),
)


def _make_aircraft(
    hex_id: str,
    alt_baro: int | str | None = None,
    alt_geom: int | str | None = None,
) -> Aircraft:
    payload = {}
    if alt_baro is not None:
        payload["alt_baro"] = alt_baro
    if alt_geom is not None:
        payload["alt_geom"] = alt_geom

    alt_feet = None
    if alt_baro == "ground":
        alt_feet = 0
    elif isinstance(alt_baro, (int, float)):
        alt_feet = int(alt_baro)

    return Aircraft(
        meta=AircraftMeta(icao_hex=hex_id, ingestor="test"),
        location=AircraftLocation(altitude_feet=alt_feet),
        direction=AircraftVector(),
        route=AircraftRoute(),
        airframe=Airframe(),
        raw=AircraftRaw(payload=payload),
    )


def test_altitude_filter_above_only():
    f = AltitudeFilter(above=20000)
    a1 = _make_aircraft("A1", alt_baro=25000)
    a2 = _make_aircraft("A2", alt_baro=20000)
    a3 = _make_aircraft("A3", alt_baro=19999)

    res = f.process([a1, a2, a3])
    hexes = [a.meta.icao_hex for a in res]
    assert hexes == ["A1", "A2"]


def test_altitude_filter_below_only():
    f = AltitudeFilter(below=10000)
    a1 = _make_aircraft("A1", alt_baro=5000)
    a2 = _make_aircraft("A2", alt_baro=10000)
    a3 = _make_aircraft("A3", alt_baro=10001)

    res = f.process([a1, a2, a3])
    hexes = [a.meta.icao_hex for a in res]
    assert hexes == ["A1", "A2"]


def test_altitude_filter_range_between():
    f = AltitudeFilter(above=2000, below=10000)
    a1 = _make_aircraft("A1", alt_baro=1000)
    a2 = _make_aircraft("A2", alt_baro=2000)
    a3 = _make_aircraft("A3", alt_baro=5000)
    a4 = _make_aircraft("A4", alt_baro=10000)
    a5 = _make_aircraft("A5", alt_baro=10001)

    res = f.process([a1, a2, a3, a4, a5])
    hexes = [a.meta.icao_hex for a in res]
    assert hexes == ["A2", "A3", "A4"]


def test_altitude_filter_invalid_bounds_raises():
    with pytest.raises(ValueError, match="'above' \\(20000\\) cannot be greater than 'below' \\(10000\\)"):
        AltitudeFilter(above=20000, below=10000)



def test_altitude_filter_invalid_source_raises():
    with pytest.raises(ValueError, match="invalid altitude_source 'invalid_src'"):
        AltitudeFilter(altitude_source="invalid_src")


def test_altitude_filter_geom_with_fallback():
    # Aircraft without geom altitude, but with baro altitude
    a_grob = _make_aircraft("GROB", alt_baro=3000, alt_geom=None)
    # Aircraft with geom altitude
    a_jet = _make_aircraft("JET", alt_baro=25000, alt_geom=25500)

    # With fallback=True (default), grob falls back to alt_baro=3000
    f_fallback = AltitudeFilter(above=2000, below=5000, altitude_source="alt_geom", fallback=True)
    res = f_fallback.process([a_grob, a_jet])
    assert [a.meta.icao_hex for a in res] == ["GROB"]

    # With fallback=False, grob is excluded because alt_geom is missing
    f_no_fallback = AltitudeFilter(above=2000, below=5000, altitude_source="alt_geom", fallback=False)
    res_no = f_no_fallback.process([a_grob, a_jet])
    assert res_no == []


def test_altitude_filter_ground_handling():
    f = AltitudeFilter(below=500)
    a_ground = _make_aircraft("GND", alt_baro="ground")
    a_air = _make_aircraft("AIR", alt_baro=1000)

    res = f.process([a_ground, a_air])
    assert [a.meta.icao_hex for a in res] == ["GND"]


def test_altitude_filter_factory_function():
    cfg = {
        "above": 10000,
        "below": 30000,
        "altitude_source": "alt_baro",
        "fallback": True,
    }
    module = get(cfg, _CTX)
    assert isinstance(module, AltitudeFilter)
    assert module._above == 10000.0
    assert module._below == 30000.0
