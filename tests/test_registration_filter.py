"""
tests/test_registration_filter.py

Tests for the registration_filter module.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from modules.registration_filter import RegistrationFilter
from schemas.aircraft import (
    Aircraft, AircraftLocation, AircraftMeta, AircraftRaw,
    AircraftRoute, AircraftVector, Airframe,
)


def _make_aircraft(hex_id: str, registration: str | None = None) -> Aircraft:
    return Aircraft(
        meta      = AircraftMeta(icao_hex=hex_id, ingestor="test", reception_type="adsb_icao"),
        location  = AircraftLocation(distance_nm=5.0),
        direction = AircraftVector(),
        route     = AircraftRoute(),
        airframe  = Airframe(registration=registration, aircraft_type=None),
        raw       = AircraftRaw(),
    )


@pytest.fixture
def setup_tar1090_db(tmp_path):
    # Set up dummy aircraft.csv
    db_dir = tmp_path / "modules" / "tar1090_db"
    db_dir.mkdir(parents=True, exist_ok=True)
    csv_file = db_dir / "aircraft.csv"
    csv_file.write_text(
        "400A0A;G-BOAC;CONC;;Concorde\n"
        "400123;G-TEST;A320;;Airbus A320\n",
        encoding="utf-8"
    )
    return tmp_path


def test_registration_filter_initialization(setup_tar1090_db):
    tmp_path = setup_tar1090_db
    rf = RegistrationFilter(["G-BOAC", "G-TEST"], tmp_path)
    assert rf._reg_to_hex.get("G-BOAC") == "400A0A"
    assert rf._reg_to_hex.get("G-TEST") == "400123"
    assert "400A0A" in rf._target_hexes
    assert "400123" in rf._target_hexes


def test_registration_filter_array_matching(setup_tar1090_db):
    tmp_path = setup_tar1090_db
    rf = RegistrationFilter(["G-BOAC", "G-TEST"], tmp_path)

    # Prepare aircraft lists
    a_boac_hex = _make_aircraft("400A0A", registration=None)         # Match G-BOAC by hex
    a_boac_reg = _make_aircraft("OTHERHEX", registration="G-BOAC")    # Match G-BOAC by reg string
    a_test_hex = _make_aircraft("400123", registration=None)         # Match G-TEST by hex
    a_nomatch  = _make_aircraft("999999", registration="G-NOMATCH")   # No match

    aircraft = [a_boac_hex, a_boac_reg, a_test_hex, a_nomatch]
    result = rf.process(aircraft)

    assert len(result) == 3
    assert a_boac_hex in result
    assert a_boac_reg in result
    assert a_test_hex in result
    assert a_nomatch not in result

    assert a_boac_hex.airframe.registration == "G-BOAC"
    assert a_boac_hex.airframe.aircraft_type == "Concorde"
    assert a_test_hex.airframe.registration == "G-TEST"
    assert a_test_hex.airframe.aircraft_type == "Airbus A320"


def test_registration_filter_empty_config(setup_tar1090_db):
    tmp_path = setup_tar1090_db
    rf = RegistrationFilter([], tmp_path)
    a_boac = _make_aircraft("400A0A", registration="G-BOAC")
    assert rf.process([a_boac]) == []
