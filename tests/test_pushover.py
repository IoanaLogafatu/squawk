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
                "message": "G-AAAA B738 : LHR -> JFK"
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
        destination_iata="JFK",
        callsign="BAW123"
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

        # Simulate > 2 hours (7300 seconds) passing by manually updating the disk timestamp/json
        past_time = time.time() - 7300  # > 2 hours ago
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

        # Third call should now go through
        display.process([a])
        assert mock_post.call_count == 2


def test_pushover_hex_and_callsign_deduplication_allows_new_callsign_within_cooldown(tmp_path):
    display = PushoverDisplay({
        "token": "valid_token",
        "user": "valid_user",
        "data_dir": str(tmp_path)
    })

    # Outbound flight
    outbound = _make_aircraft(
        "407F0D",
        registration="G-RUKK",
        aircraft_type="737-8AS",
        origin_iata="STN",
        destination_iata="LGW",
        callsign="RYR1505"
    )

    # Return flight 30 minutes later with same physical aircraft (hex) but new callsign
    return_flight = _make_aircraft(
        "407F0D",
        registration="G-RUKK",
        aircraft_type="737-8AS",
        origin_iata="LGW",
        destination_iata="EDI",
        callsign="RYR1606"
    )

    with patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_post.return_value = mock_resp

        # 1. First flight triggers notification
        display.process([outbound])
        assert mock_post.call_count == 1

        # 2. Duplicate detection of same flight (same hex & callsign) within 2 hours -> blocked
        display.process([outbound])
        assert mock_post.call_count == 1

        # 3. Same aircraft but new callsign (turnaround flight 30 minutes later) -> allowed!
        display.process([return_flight])
        assert mock_post.call_count == 2

        # 4. Duplicate check for return flight -> blocked
        display.process([return_flight])
        assert mock_post.call_count == 2


def test_pushover_suppresses_duplicate_when_callsign_resolves_3_minutes_later(tmp_path):
    display = PushoverDisplay({
        "token": "valid_token",
        "user": "valid_user",
        "data_dir": str(tmp_path)
    })

    # Aircraft detected initially without callsign
    a1 = _make_aircraft(
        "407F0D",
        registration="EI-EKM",
        aircraft_type="737-8AS",
        origin_iata="NRN",
        destination_iata="EDI",
        callsign=None
    )

    # Same aircraft 3 minutes later after callsign is populated
    a2 = _make_aircraft(
        "407F0D",
        registration="EI-EKM",
        aircraft_type="737-8AS",
        origin_iata="NRN",
        destination_iata="EDI",
        callsign="RYN23823"
    )

    with patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_post.return_value = mock_resp

        # 1. Initial notification sent at 10:53 (callsign empty)
        display.process([a1])
        assert mock_post.call_count == 1

        # 2. Notification at 10:56 (callsign now populated) -> suppressed because 3 min < 15 min empty callsign window!
        display.process([a2])
        assert mock_post.call_count == 1


def test_pushover_custom_cooldown_setting(tmp_path):
    display = PushoverDisplay({
        "token": "valid_token",
        "user": "valid_user",
        "cooldown_seconds": 300,
        "data_dir": str(tmp_path)
    })

    a = _make_aircraft(
        "AA1111",
        registration="G-AAAA",
        aircraft_type="B738",
        origin_iata="LHR",
        destination_iata="JFK",
        callsign="BAW123"
    )

    json_file = tmp_path / "display" / "pushover" / "last_notification.json"

    with patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_post.return_value = mock_resp

        display.process([a])
        assert mock_post.call_count == 1

        # Immediately blocked
        display.process([a])
        assert mock_post.call_count == 1

        # Advance timestamp by 310 seconds (over the custom 300s cooldown)
        if json_file.exists():
            import json
            jdata = json.loads(json_file.read_text(encoding="utf-8"))
            past_time = time.time() - 310
            jdata["timestamp"] = past_time
            if "entries" in jdata and "AA1111_BAW123" in jdata["entries"]:
                jdata["entries"]["AA1111_BAW123"]["timestamp"] = past_time
            json_file.write_text(json.dumps(jdata), encoding="utf-8")

        # Now allowed
        display.process([a])
        assert mock_post.call_count == 2


def test_pushover_sends_notification_with_airline_prefix(tmp_path):
    display = PushoverDisplay({
        "token": "valid_token",
        "user": "valid_user",
        "data_dir": str(tmp_path)
    })

    a = _make_aircraft(
        "407F0D",
        registration="G-RUKK",
        aircraft_type="737-8AS",
        origin_iata="FEZ",
        destination_iata="STN",
        callsign="RYR1505"
    )
    a.route.airline_name = "Ryanair"
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
                "message": "Ryanair G-RUKK 737-8AS : FEZ (Morocco) -> STN (United Kingdom) [RYR1505]"
            },
            timeout=5
        )


def test_pushover_suppresses_incomplete_route_messages(tmp_path):
    display = PushoverDisplay({
        "token": "valid_token",
        "user": "valid_user",
        "data_dir": str(tmp_path)
    })

    # Aircraft without origin/destination
    a1 = _make_aircraft(
        "4D2221",
        registration="9H-QCH",
        aircraft_type="737NG 8AS/W",
        origin_iata=None,
        destination_iata=None
    )
    a1.airframe.operator = "Malta Air"

    # Aircraft with origin/destination populated
    a2 = _make_aircraft(
        "4D2221",
        registration="9H-QCH",
        aircraft_type="737NG 8AS/W",
        origin_iata="MLA",
        destination_iata="BVA"
    )
    a2.airframe.operator = "Malta Air"
    a2.route.origin_country = "Malta"
    a2.route.destination_country = "France"

    messages = []

    def mock_post(url, data=None, timeout=None):
        messages.append(data.get("message"))
        resp = MagicMock()
        resp.status_code = 200
        return resp

    with patch("requests.post", side_effect=mock_post):
        # 1. Process incomplete aircraft (suppressed, no message sent)
        display.process([a1])
        assert len(messages) == 0

        # 2. Process complete aircraft (sends message)
        display.process([a2])
        assert len(messages) == 1
        assert messages[0] == "Malta Air 9H-QCH 737NG 8AS/W : MLA (Malta) -> BVA (France)"

        # 3. Process complete aircraft again immediately (rate limited)
        display.process([a2])
        assert len(messages) == 1


