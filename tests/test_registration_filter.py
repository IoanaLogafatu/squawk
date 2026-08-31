"""
tests/test_registration_filter.py

Tests for the registration_filter module.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from config import ObserverConfig
from modules import ModuleContext
from modules.registration_filter import RegistrationFilter, get
from schemas.aircraft import (
    Aircraft, AircraftLocation, AircraftMeta, AircraftRaw,
    AircraftRoute, AircraftVector, Airframe,
)

# registration_filter ignores ctx entirely — any placeholder will do.
_CTX = ModuleContext(
    data_dir=Path("."),
    module_dir=Path("./modules/registration_filter"),
    observer=ObserverConfig(latitude=0.0, longitude=0.0),
)


def _make_aircraft(hex_id: str, registration: str | None = None) -> Aircraft:
    return Aircraft(
        meta      = AircraftMeta(icao_hex=hex_id, ingestor="test", reception_type="adsb_icao"),
        location  = AircraftLocation(distance_nm=5.0),
        direction = AircraftVector(),
        route     = AircraftRoute(),
        airframe  = Airframe(registration=registration),
        raw       = AircraftRaw(),
    )


def test_registration_filter_initialization():
    rf_list = RegistrationFilter(["g-boac", "G-TEST "])
    assert rf_list._target_registrations == {"G-BOAC", "G-TEST"}

    rf_str = RegistrationFilter("g-boac")
    assert rf_str._target_registrations == {"G-BOAC"}


def test_registration_filter_matching():
    rf = RegistrationFilter(["G-BOAC", "G-TEST"])

    a_boac = _make_aircraft("400A0A", registration="g-boac")
    a_test = _make_aircraft("400123", registration="G-TEST")
    a_nomatch = _make_aircraft("999999", registration="G-NOMATCH")

    result = rf.process([a_boac, a_test, a_nomatch])

    assert len(result) == 2
    assert a_boac in result
    assert a_test in result
    assert a_nomatch not in result


def test_registration_filter_none_registration():
    """Verify an aircraft with airframe.registration = None passed directly to process() is filtered out without crashing."""
    rf = RegistrationFilter(["G-BOAC"])
    a_none = _make_aircraft("400A0A", registration=None)
    result = rf.process([a_none])
    assert result == []


def test_registration_filter_empty_config():
    rf = RegistrationFilter([])
    a_boac = _make_aircraft("400A0A", registration="G-BOAC")
    assert rf.process([a_boac]) == []


def test_registration_filter_factory():
    rf = get({"registrations": ["G-BOAC"]}, _CTX)
    assert rf._target_registrations == {"G-BOAC"}

