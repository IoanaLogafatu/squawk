"""
tests/test_pushover.py

Tests for the Pushover display module.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from config import ObserverConfig
from display.pushover import PushoverDisplay
from modules import ModuleContext
from schemas.aircraft import (
    Aircraft, AircraftLocation, AircraftMeta, AircraftRaw,
    AircraftRoute, AircraftVector, Airframe,
)


def _ctx(tmp_path: Path) -> ModuleContext:
    return ModuleContext(
        data_dir=tmp_path,
        module_dir=tmp_path / "display" / "pushover",
        observer=ObserverConfig(latitude=53.7778, longitude=-1.5721),
    )


def _make_aircraft(
    hex_id: str,
    registration: str | None = "G-AAAA",
    aircraft_type: str | None = "B738",
    origin_iata: str | None = "LHR",
    destination_iata: str | None = "JFK",
    callsign: str | None = "BAW123",
    airline_name: str | None = "British Airways"
) -> Aircraft:
    return Aircraft(
        meta      = AircraftMeta(icao_hex=hex_id, ingestor="test", reception_type="adsb_icao"),
        location  = AircraftLocation(distance_nm=5.0),
        direction = AircraftVector(),
        route     = AircraftRoute(
            callsign=callsign,
            origin_iata=origin_iata,
            destination_iata=destination_iata,
            airline_name=airline_name
        ),
        airframe  = Airframe(registration=registration, aircraft_type=aircraft_type),
        raw       = AircraftRaw(),
    )


def test_pushover_skipped_on_placeholder_or_missing_credentials(tmp_path):
    display1 = PushoverDisplay({}, _ctx(tmp_path))
    display2 = PushoverDisplay({
        "token": "xxxxxxxxxxxxxxxxxxxxxxxxxx",
        "user": "xxxxxxxxxxxxxxxxxxxxxxxxxx"
    }, _ctx(tmp_path))

    a = _make_aircraft("AA1111")

    with patch("requests.post") as mock_post:
        display1.process([a])
        display2.process([a])
        mock_post.assert_not_called()


def test_pushover_skipped_on_empty_aircraft_list(tmp_path):
    display = PushoverDisplay({
        "token": "valid_token",
        "user": "valid_user"
    }, _ctx(tmp_path))

    with patch("requests.post") as mock_post:
        result = display.process([])
        assert result == []
        mock_post.assert_not_called()


def test_pushover_sends_notification_with_correct_details_and_format(tmp_path):
    display = PushoverDisplay({
        "token": "valid_token",
        "user": "valid_user"
    }, _ctx(tmp_path))

    a = _make_aircraft(
        "407F0D",
        registration="G-RUKK",
        aircraft_type="737-8AS",
        origin_iata="FEZ",
        destination_iata="STN",
        callsign="RYR1505",
        airline_name="Ryanair"
    )
    a.route.origin_country = "Morocco"
    a.route.destination_country = "United Kingdom"

    with patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        display.process([a])

        mock_post.assert_called_once_with(
            "https://api.pushover.net/1/messages.json",
            data={
                "token": "valid_token",
                "user": "valid_user",
                "message": "Ryanair G-RUKK [RYR1505] 737-8AS  :  FEZ (Morocco) -> STN (UK)"
            },
            timeout=5
        )

        ts_file = tmp_path / "display" / "pushover" / "last_notification.txt"
        assert ts_file.exists()
        timestamp = float(ts_file.read_text(encoding="utf-8").strip())
        assert time.time() - timestamp < 5.0


def test_pushover_requires_all_5_facts(tmp_path):
    display = PushoverDisplay({
        "token": "valid_token",
        "user": "valid_user"
    }, _ctx(tmp_path))

    with patch("requests.post") as mock_post:
        # Missing airline
        a1 = _make_aircraft("A1", airline_name=None)
        display.process([a1])
        mock_post.assert_not_called()

        # Missing registration
        a2 = _make_aircraft("A2", registration=None)
        display.process([a2])
        mock_post.assert_not_called()

        # Missing callsign
        a3 = _make_aircraft("A3", callsign=None)
        display.process([a3])
        mock_post.assert_not_called()

        # Missing aircraft type
        a4 = _make_aircraft("A4", aircraft_type=None)
        display.process([a4])
        mock_post.assert_not_called()

        # Missing origin
        a5 = _make_aircraft("A5", origin_iata=None)
        display.process([a5])
        mock_post.assert_not_called()

        # Missing destination
        a6 = _make_aircraft("A6", destination_iata=None)
        display.process([a6])
        mock_post.assert_not_called()


def test_pushover_rate_limit_checks_disk(tmp_path):
    display = PushoverDisplay({
        "token": "valid_token",
        "user": "valid_user"
    }, _ctx(tmp_path))

    a = _make_aircraft("AA1111")
    ts_file = tmp_path / "display" / "pushover" / "last_notification.txt"

    with patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_post.return_value = mock_resp

        # First call succeeds
        display.process([a])
        assert mock_post.call_count == 1
        assert ts_file.exists()

        # Second call immediately should be rate limited
        display.process([a])
        assert mock_post.call_count == 1

        # Simulate > 2 hours passing
        past_time = time.time() - 7300
        ts_file.parent.mkdir(parents=True, exist_ok=True)
        ts_file.write_text(str(past_time), encoding="utf-8")
        json_file = tmp_path / "display" / "pushover" / "last_notification.json"
        if json_file.exists():
            import json
            jdata = json.loads(json_file.read_text(encoding="utf-8"))
            jdata["timestamp"] = past_time
            if "entries" in jdata and "AA1111_BAW123" in jdata["entries"]:
                jdata["entries"]["AA1111_BAW123"]["timestamp"] = past_time
            json_file.write_text(json.dumps(jdata), encoding="utf-8")

        # Third call should go through
        display.process([a])
        assert mock_post.call_count == 2


def test_pushover_hex_and_callsign_deduplication_allows_new_callsign_within_cooldown(tmp_path):
    display = PushoverDisplay({
        "token": "valid_token",
        "user": "valid_user"
    }, _ctx(tmp_path))

    outbound = _make_aircraft(
        "407F0D",
        registration="G-RUKK",
        aircraft_type="737-8AS",
        origin_iata="STN",
        destination_iata="LGW",
        callsign="RYR1505",
        airline_name="Ryanair"
    )

    return_flight = _make_aircraft(
        "407F0D",
        registration="G-RUKK",
        aircraft_type="737-8AS",
        origin_iata="LGW",
        destination_iata="EDI",
        callsign="RYR1606",
        airline_name="Ryanair"
    )

    with patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_post.return_value = mock_resp

        # 1. First flight triggers notification
        display.process([outbound])
        assert mock_post.call_count == 1

        # 2. Duplicate flight (same hex & callsign) blocked
        display.process([outbound])
        assert mock_post.call_count == 1

        # 3. New turnaround flight with new callsign allowed
        display.process([return_flight])
        assert mock_post.call_count == 2
