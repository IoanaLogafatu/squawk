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
    rf = RegistrationFilter("http://dummy-url", tmp_path)
    assert rf._reg_to_hex.get("G-BOAC") == "400A0A"
    assert rf._reg_to_hex.get("G-TEST") == "400123"


def test_registration_filter_fetches_and_filters(setup_tar1090_db):
    tmp_path = setup_tar1090_db
    rf = RegistrationFilter("http://dummy-url", tmp_path)

    # Prepare aircraft lists
    a_conc_hex = _make_aircraft("400A0A", registration=None)  # Match by hex code mapping
    a_conc_reg = _make_aircraft("OTHERHEX", registration="G-BOAC")  # Match by registration string directly
    a_test     = _make_aircraft("400123", registration="G-TEST")  # No match
    aircraft = [a_conc_hex, a_conc_reg, a_test]

    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.text = "G-BOAC\n"
        mock_resp.status_code = 200
        mock_get.return_value = mock_resp

        # Process cycle
        result = rf.process(aircraft)

        mock_get.assert_called_once_with("http://dummy-url", timeout=10)
        assert len(result) == 2
        assert a_conc_hex in result
        assert a_conc_reg in result
        assert a_test not in result

        # Check caching
        reg_file = tmp_path / "modules" / "registration_filter" / "registration.txt"
        assert reg_file.exists()
        assert reg_file.read_text(encoding="utf-8") == "G-BOAC"


def test_registration_filter_rate_limiting(setup_tar1090_db):
    tmp_path = setup_tar1090_db
    rf = RegistrationFilter("http://dummy-url", tmp_path)

    a_conc = _make_aircraft("400A0A")
    aircraft = [a_conc]

    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.text = "G-BOAC"
        mock_resp.status_code = 200
        mock_get.return_value = mock_resp

        # First check (fetches because no file exists)
        rf.process(aircraft)
        assert mock_get.call_count == 1

        # Second check immediately (skips fetch)
        rf.process(aircraft)
        assert mock_get.call_count == 1

        # Simulate 16 minutes passing
        ts_file = tmp_path / "modules" / "registration_filter" / "last_check.txt"
        past_time = time.time() - 960
        ts_file.write_text(str(past_time), encoding="utf-8")

        # Third check (fetches again)
        rf.process(aircraft)
        assert mock_get.call_count == 2


def test_registration_filter_fetch_failure_falls_back_to_cache(setup_tar1090_db):
    tmp_path = setup_tar1090_db
    
    # Pre-populate cache
    cache_dir = tmp_path / "modules" / "registration_filter"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "registration.txt").write_text("G-BOAC", encoding="utf-8")
    
    rf = RegistrationFilter("http://dummy-url", tmp_path)

    a_conc = _make_aircraft("400A0A")
    a_test = _make_aircraft("400123")
    aircraft = [a_conc, a_test]

    with patch("requests.get") as mock_get:
        mock_get.side_effect = Exception("Network Down")

        # Process cycle (fetch fails, falls back to G-BOAC cache)
        result = rf.process(aircraft)

        assert mock_get.call_count == 1
        assert len(result) == 1
        assert result[0] == a_conc
