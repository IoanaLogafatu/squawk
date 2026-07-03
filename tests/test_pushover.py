"""
tests/test_pushover.py

Tests for the Pushover display module.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from display.pushover import PushoverDisplay
from schemas.aircraft import (
    Aircraft, AircraftLocation, AircraftMeta, AircraftRaw,
    AircraftRoute, AircraftVector, Airframe,
)


def _make_aircraft(
    hex_id: str,
    registration: str | None = None,
    aircraft_type: str | None = None,
    origin_iata: str | None = None,
    destination_iata: str | None = None,
    callsign: str | None = None
) -> Aircraft:
    return Aircraft(
        meta      = AircraftMeta(icao_hex=hex_id, ingestor="test", reception_type="adsb_icao"),
        location  = AircraftLocation(distance_nm=5.0),
        direction = AircraftVector(),
        route     = AircraftRoute(
            callsign=callsign,
            origin_iata=origin_iata,
            destination_iata=destination_iata
        ),
        airframe  = Airframe(registration=registration, aircraft_type=aircraft_type),
        raw       = AircraftRaw(),
    )


def test_pushover_skipped_on_placeholder_or_missing_credentials(tmp_path):
    # Missing credentials
    display1 = PushoverDisplay({"data_dir": str(tmp_path)})
    # Placeholder credentials
    display2 = PushoverDisplay({
        "token": "xxxxxxxxxxxxxxxxxxxxxxxxxx",
        "user": "xxxxxxxxxxxxxxxxxxxxxxxxxx",
        "data_dir": str(tmp_path)
    })

    a = _make_aircraft("AA1111", registration="G-AAAA")

    with patch("requests.post") as mock_post:
        display1.process([a])
        display2.process([a])
        mock_post.assert_not_called()


def test_pushover_skipped_on_empty_aircraft_list(tmp_path):
    display = PushoverDisplay({
        "token": "valid_token",
        "user": "valid_user",
        "data_dir": str(tmp_path)
    })

    with patch("requests.post") as mock_post:
        result = display.process([])
        assert result == []
        mock_post.assert_not_called()


def test_pushover_sends_notification_with_correct_details(tmp_path):
    display = PushoverDisplay({
        "token": "valid_token",
        "user": "valid_user",
        "data_dir": str(tmp_path)
    })

    a = _make_aircraft(
        "AA1111",
        registration="G-AAAA",
        aircraft_type="B738",
        origin_iata="LHR",
        destination_iata="JFK"
    )

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
                "message": "G-AAAA B738 LHR JFK"
            },
            timeout=5
        )

        # Check that the timestamp file was written
        ts_file = tmp_path / "display" / "pushover" / "last_notification.txt"
        assert ts_file.exists()
        timestamp = float(ts_file.read_text(encoding="utf-8").strip())
        assert time.time() - timestamp < 5.0


def test_pushover_rate_limit_checks_disk(tmp_path):
    display = PushoverDisplay({
        "token": "valid_token",
        "user": "valid_user",
        "data_dir": str(tmp_path)
    })

    a = _make_aircraft(
        "AA1111",
        registration="G-AAAA",
        aircraft_type="B738",
        origin_iata="LHR",
        destination_iata="JFK"
    )

    ts_file = tmp_path / "display" / "pushover" / "last_notification.txt"

    with patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_post.return_value = mock_resp

        # First call succeeds and writes timestamp
        display.process([a])
        assert mock_post.call_count == 1
        assert ts_file.exists()

        # Second call immediately should be rate limited
        display.process([a])
        assert mock_post.call_count == 1  # count should still be 1

        # Simulate 16 minutes passing by manually updating the disk timestamp
        past_time = time.time() - 960  # 16 minutes ago
        ts_file.parent.mkdir(parents=True, exist_ok=True)
        ts_file.write_text(str(past_time), encoding="utf-8")

        # Third call should now go through
        display.process([a])
        assert mock_post.call_count == 2
