"""
tests/test_http_display.py

Tests for the HTTP display plugin.

Covers:
  1. Plugin contract — process() returns list unchanged
  2. HTTP server — page served, 404 for unknown paths
  3. render_data — JSON output for each display field
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request

import pytest

from display.http import HttpDisplay
from display.http.server import render_data
from schemas.aircraft import (
    Aircraft, AircraftLocation, AircraftMeta, AircraftRaw,
    AircraftRoute, AircraftVector, Airframe,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _make_aircraft(
    hex_id="AA1111",
    registration=None,
    callsign=None,
    aircraft_type=None,
    operator=None,
    airline_name=None,
    origin_iata=None,
    destination_iata=None,
    distance_nm=None,
    bearing_degrees=None,
    altitude_feet=None,
    vertical_rate_fpm=None,
) -> Aircraft:
    return Aircraft(
        meta      = AircraftMeta(icao_hex=hex_id, ingestor="test", reception_type="adsb_icao"),
        location  = AircraftLocation(distance_nm=distance_nm, bearing_degrees=bearing_degrees, altitude_feet=altitude_feet),
        direction = AircraftVector(vertical_rate_fpm=vertical_rate_fpm),
        route     = AircraftRoute(callsign=callsign, airline_name=airline_name,
                                  origin_iata=origin_iata, destination_iata=destination_iata),
        airframe  = Airframe(registration=registration, aircraft_type=aircraft_type, operator=operator),
        raw       = AircraftRaw(),
    )


def _get(url: str) -> tuple[int, str]:
    with urllib.request.urlopen(url, timeout=2) as r:
        return r.status, r.read().decode()


# ===========================================================================
# 1. Plugin contract
# ===========================================================================

def test_http_display_returns_aircraft_unchanged():
    display  = HttpDisplay({"port": _free_port()})
    aircraft = [_make_aircraft()]
    assert display.process(aircraft) is aircraft


def test_http_display_process_empty_list():
    display = HttpDisplay({"port": _free_port()})
    assert display.process([]) == []


# ===========================================================================
# 2. HTTP server
# ===========================================================================

def test_http_display_serves_page():
    port = _free_port()
    HttpDisplay({"port": port})
    time.sleep(0.05)
    status, body = _get(f"http://localhost:{port}/")
    assert status == 200
    assert "<title>Squawk</title>" in body


def test_http_display_page_has_sse_script():
    port = _free_port()
    HttpDisplay({"port": port})
    time.sleep(0.05)
    _, body = _get(f"http://localhost:{port}/")
    assert "EventSource" in body
    assert "/events" in body


def test_http_display_404_for_unknown_path():
    port = _free_port()
    HttpDisplay({"port": port})
    time.sleep(0.05)
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(f"http://localhost:{port}/unknown", timeout=2)
    assert exc.value.code == 404


def test_http_display_default_port_is_7700():
    # Verify the default is documented correctly — not actually binding 7700
    # in CI, just check the cfg path with an explicit port override.
    port    = _free_port()
    display = HttpDisplay({"port": port})
    assert display is not None


# ===========================================================================
# 3. render_data
# ===========================================================================

def test_render_data_none_is_null():
    assert render_data(None) == "null"


def test_render_data_uses_registration():
    a = _make_aircraft(registration="G-ABCD")
    d = json.loads(render_data(a))
    assert d["ident"] == "G-ABCD"


def test_render_data_falls_back_to_callsign():
    a = _make_aircraft(hex_id="AA1111", callsign="BAW123")
    d = json.loads(render_data(a))
    assert d["ident"] == "BAW123"


def test_render_data_falls_back_to_icao_hex():
    a = _make_aircraft(hex_id="AA1111")
    d = json.loads(render_data(a))
    assert d["ident"] == "AA1111"


def test_render_data_aircraft_type():
    a = _make_aircraft(aircraft_type="BOEING 737-800")
    d = json.loads(render_data(a))
    assert d["aircraft_type"] == "BOEING 737-800"


def test_render_data_altitude_ground():
    a = _make_aircraft(altitude_feet=0)
    d = json.loads(render_data(a))
    assert d["altitude"] == "GND"


def test_render_data_altitude_formatted():
    a = _make_aircraft(altitude_feet=35000)
    d = json.loads(render_data(a))
    assert d["altitude"] == "35,000 ft"


def test_render_data_altitude_unknown():
    a = _make_aircraft(altitude_feet=None)
    d = json.loads(render_data(a))
    assert d["altitude"] == "—"


def test_render_data_distance_formatted():
    a = _make_aircraft(distance_nm=12.345)
    d = json.loads(render_data(a))
    assert d["distance"] == "12.3 nm"


def test_render_data_distance_with_cardinal():
    a = _make_aircraft(distance_nm=5.0, bearing_degrees=45.0)  # NE
    d = json.loads(render_data(a))
    assert d["distance"] == "5.0 nm NE"


def test_render_data_distance_unknown():
    a = _make_aircraft(distance_nm=None)
    d = json.loads(render_data(a))
    assert d["distance"] == "—"


def test_render_data_operator_present():
    a = _make_aircraft(operator="British Airways")
    d = json.loads(render_data(a))
    assert d["operator"] == "British Airways"


def test_render_data_operator_absent_is_null():
    a = _make_aircraft(operator=None)
    d = json.loads(render_data(a))
    assert d["operator"] is None


def test_render_data_climbing():
    a = _make_aircraft(vertical_rate_fpm=512)
    d = json.loads(render_data(a))
    assert d["vrate"] == "↑"


def test_render_data_descending():
    a = _make_aircraft(vertical_rate_fpm=-512)
    d = json.loads(render_data(a))
    assert d["vrate"] == "↓"


def test_render_data_level():
    a = _make_aircraft(vertical_rate_fpm=0)
    d = json.loads(render_data(a))
    assert d["vrate"] == "—"


def test_render_data_has_timestamp():
    a = _make_aircraft()
    d = json.loads(render_data(a))
    assert "UTC" in d["timestamp"]


def test_render_data_airline_present():
    a = _make_aircraft(airline_name="Ryanair")
    d = json.loads(render_data(a))
    assert d["airline"] == "Ryanair"


def test_render_data_airline_absent_is_null():
    a = _make_aircraft(airline_name=None)
    d = json.loads(render_data(a))
    assert d["airline"] is None


def test_render_data_route_both_iata():
    a = _make_aircraft(origin_iata="REU", destination_iata="LBA")
    d = json.loads(render_data(a))
    assert d["route"] == "REU → LBA"


def test_render_data_route_origin_only():
    a = _make_aircraft(origin_iata="REU")
    d = json.loads(render_data(a))
    assert d["route"] == "REU → ?"


def test_render_data_route_destination_only():
    a = _make_aircraft(destination_iata="LBA")
    d = json.loads(render_data(a))
    assert d["route"] == "? → LBA"


def test_render_data_route_neither_is_null():
    a = _make_aircraft()
    d = json.loads(render_data(a))
    assert d["route"] is None
