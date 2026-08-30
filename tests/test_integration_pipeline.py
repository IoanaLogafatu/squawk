"""
tests/test_integration_pipeline.py

Integration test for the processor chain: registration_filter -> adsbdb -> pushover.

tar1090_db enrichment now runs at ingest (see brief-tar1090-to-ingest.md), so an
aircraft reaches the processor chain with airframe.registration already populated.
This test asserts the filter/enrichment/display slice downstream of ingest.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from config import ObserverConfig
from display.pushover import PushoverDisplay
from modules import ModuleContext
from modules.adsbdb import AdsbdbEnricher
from modules.registration_filter import RegistrationFilter
from schemas.aircraft import (
    Aircraft, AircraftLocation, AircraftMeta, AircraftRaw,
    AircraftRoute, AircraftVector, Airframe,
)


def test_pipeline_ingest_enriched_aircraft_to_pushover_notification(tmp_path):
    ctx = ModuleContext(
        data_dir=tmp_path,
        module_dir=tmp_path / "display" / "pushover",
        observer=ObserverConfig(latitude=53.7778, longitude=-1.5721),
    )
    reg_filter = RegistrationFilter(["G-RUKK", "G-EZOK"])
    adsbdb = AdsbdbEnricher(cache_dir=tmp_path / "modules" / "adsbdb")
    pushover = PushoverDisplay({
        "token": "valid_token_123",
        "user": "valid_user_456",
    }, ctx)

    # Ingest-time enrichment has already filled registration + aircraft_type.
    a = Aircraft(
        meta=AircraftMeta(icao_hex="407F0D", ingestor="personal_adsb"),
        location=AircraftLocation(),
        direction=AircraftVector(),
        route=AircraftRoute(callsign="RYR1505"),
        airframe=Airframe(registration="G-RUKK", aircraft_type="BOEING 737-800"),
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
        filtered = reg_filter.process([a])
        assert len(filtered) == 1
        assert filtered[0].airframe.registration == "G-RUKK"

        enriched = adsbdb.process(filtered)
        assert enriched[0].airframe.aircraft_type == "737-8AS"
        assert enriched[0].route.origin_iata == "FEZ"
        assert enriched[0].route.destination_iata == "STN"
        assert enriched[0].route.airline_name == "Ryanair"

        pushover.process(enriched)
        assert len(sent_messages) == 1
        assert sent_messages[0] == "Ryanair G-RUKK [RYR1505] 737-8AS  :  FEZ (Morocco) -> STN (UK)"


