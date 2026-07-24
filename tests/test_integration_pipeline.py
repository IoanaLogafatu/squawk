"""
tests/test_integration_pipeline.py

Integration test for the pipeline: registration_filter -> adsbdb -> pushover.
Verifies that aircraft identified by hex code without initial registration details
are enriched to output formatted notifications like 'Ryanair G-RUKK 737-8AS FEZ (Morocco) -> STN (United Kingdom)'.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from display.pushover import PushoverDisplay
from modules.adsbdb import AdsbdbEnricher
from modules.registration_filter import RegistrationFilter
from schemas.aircraft import (
    Aircraft, AircraftLocation, AircraftMeta, AircraftRaw,
    AircraftRoute, AircraftVector, Airframe,
)


def test_pipeline_hex_aircraft_to_pushover_notification(tmp_path):
    # 1. Setup tar1090 DB fixture
    db_dir = tmp_path / "modules" / "tar1090_db"
    db_dir.mkdir(parents=True, exist_ok=True)
    csv_file = db_dir / "aircraft.csv"
    csv_file.write_text(
        "407F0D;G-RUKK;B738;;BOEING 737-800\n"
        "4067EE;G-EZOK;A320;;Airbus A320\n",
        encoding="utf-8"
    )

    reg_filter = RegistrationFilter(["G-RUKK", "G-EZOK"], tmp_path)
    adsbdb = AdsbdbEnricher(cache_dir=tmp_path / "modules" / "adsbdb")
    pushover = PushoverDisplay({
        "token": "valid_token_123",
        "user": "valid_user_456",
        "data_dir": str(tmp_path)
    })

    # Incoming raw aircraft record from ADS-B (hex only, no registration or route)
    a = Aircraft(
        meta=AircraftMeta(icao_hex="407F0D", ingestor="personal_adsb"),
        location=AircraftLocation(),
        direction=AircraftVector(),
        route=AircraftRoute(callsign="RYR1505"),
        airframe=Airframe(),
        raw=AircraftRaw(),
    )

    adsbdb_api_response = {
        "response": {
            "aircraft": {
                "type": "737-8AS",
                "icao_type": "B738",
                "manufacturer": "Boeing",
                "mode_s": "407F0D",
                "registration": "G-RUKK",
            },
            "flightroute": {
                "callsign": "RYR1505",
                "airline": {"name": "Ryanair", "icao": "RYR"},
                "origin": {"iata_code": "FEZ", "name": "Fes Saïss", "country_name": "Morocco"},
                "destination": {"iata_code": "STN", "name": "London Stansted", "country_name": "United Kingdom"}
            }
        }
    }

    sent_messages = []

    def mock_post(url, data=None, timeout=None):
        sent_messages.append(data.get("message"))
        resp = MagicMock()
        resp.status_code = 200
        return resp

    def mock_get(url, headers=None, timeout=None):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = adsbdb_api_response
        return resp

    with patch("requests.get", side_effect=mock_get), patch("requests.post", side_effect=mock_post):
        # Step 1: Filter
        filtered = reg_filter.process([a])
        assert len(filtered) == 1
        assert filtered[0].airframe.registration == "G-RUKK"

        # Step 2: Enrich
        enriched = adsbdb.process(filtered)
        assert enriched[0].airframe.aircraft_type == "737-8AS"
        assert enriched[0].route.origin_iata == "FEZ"
        assert enriched[0].route.destination_iata == "STN"
        assert enriched[0].route.airline_name == "Ryanair"

        # Step 3: Pushover Display
        pushover.process(enriched)
        assert len(sent_messages) == 1
        assert sent_messages[0] == "Ryanair G-RUKK 737-8AS FEZ (Morocco) -> STN (United Kingdom)"
