"""
tests/test_http_display.py

Tests for the HTTP display module.

Covers:
  1. Module contract — process() returns list unchanged
  2. HTTP server — page served, 404 for unknown paths
  3. Panel config — chain_name → title/order lookup, defaults, list payload
  4. render_aircraft_dict — JSON output for each display field
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request

import pytest

from display.http import HttpDisplay
from display.http.server import render_aircraft_dict
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
# 1. Module contract
# ===========================================================================

def test_http_display_returns_aircraft_unchanged():
    display  = HttpDisplay({"port": _free_port(), "chain_name": "t"})
    aircraft = [_make_aircraft()]
    assert display.process(aircraft) is aircraft


def test_http_display_process_empty_list():
    display = HttpDisplay({"port": _free_port(), "chain_name": "t"})
    assert display.process([]) == []


# ===========================================================================
# 2. HTTP server
# ===========================================================================

def test_http_display_serves_page():
    port = _free_port()
    HttpDisplay({"port": port, "chain_name": "t"})
    time.sleep(0.05)
    status, body = _get(f"http://localhost:{port}/")
    assert status == 200
    assert "<title>Squawk</title>" in body


def test_http_display_page_has_sse_script():
    port = _free_port()
    HttpDisplay({"port": port, "chain_name": "t"})
    time.sleep(0.05)
    _, body = _get(f"http://localhost:{port}/")
    assert "EventSource" in body
    assert "/events" in body


def test_http_display_404_for_unknown_path():
    port = _free_port()
    HttpDisplay({"port": port, "chain_name": "t"})
    time.sleep(0.05)
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(f"http://localhost:{port}/unknown", timeout=2)
    assert exc.value.code == 404


def test_http_display_default_port_is_7700():
    port    = _free_port()
    display = HttpDisplay({"port": port, "chain_name": "t"})
    assert display is not None


# ===========================================================================
# 3. Panel config
# ===========================================================================

def test_http_display_multi_panel_updates():
    port = _free_port()
    panels = {
        "low_level":  {"title": "Below 10k",  "order": 1},
        "high_level": {"title": "Above 10k", "order": 2},
    }
    display_low  = HttpDisplay({"port": port, "chain_name": "low_level",  "panels": panels})
    display_high = HttpDisplay({"port": port, "chain_name": "high_level", "panels": panels})

    a_low  = _make_aircraft(hex_id="111111", registration="G-LOWW", altitude_feet=4000)
    a_high = _make_aircraft(hex_id="222222", registration="G-HIGH", altitude_feet=32000)

    display_low.process([a_low])
    display_high.process([a_high])

    time.sleep(0.05)
    status, body = _get(f"http://localhost:{port}/api/status")
    assert status == 200
    data = json.loads(body)
    assert "panels" in data
    assert data["panels"]["low_level"]["aircraft"][0]["ident"]  == "G-LOWW"
    assert data["panels"]["high_level"]["aircraft"][0]["ident"] == "G-HIGH"


def test_http_display_reads_title_and_order_from_panel_config():
    panels = {"low_level": {"title": "Approach & Low", "order": 1}}
    display = HttpDisplay({"port": _free_port(), "chain_name": "low_level", "panels": panels})
    assert display.panel_title == "Approach & Low"
    assert display.panel_order == 1


def test_http_display_missing_panel_block_falls_back(capsys):
    display = HttpDisplay({"port": _free_port(), "chain_name": "some_new_chain", "panels": {}})
    captured = capsys.readouterr()
    assert display.panel_title == "Some New Chain"
    assert display.panel_order == 999
    assert "no panel config for chain 'some_new_chain'" in captured.out


def test_http_display_payload_carries_full_aircraft_list():
    port = _free_port()
    display = HttpDisplay({"port": port, "chain_name": "list_chain", "panels": {}})
    aircraft = [
        _make_aircraft(hex_id="AAAA01", registration="REG-1"),
        _make_aircraft(hex_id="AAAA02", registration="REG-2"),
        _make_aircraft(hex_id="AAAA03", registration="REG-3"),
    ]
    display.process(aircraft)

    time.sleep(0.05)
    _, body = _get(f"http://localhost:{port}/api/status")
    panel = json.loads(body)["panels"]["list_chain"]
    assert panel["count"] == 3
    assert len(panel["aircraft"]) == 3
    assert [a["ident"] for a in panel["aircraft"]] == ["REG-1", "REG-2", "REG-3"]


def test_http_display_empty_chain_is_empty_list_not_null():
    port = _free_port()
    display = HttpDisplay({"port": port, "chain_name": "empty_chain", "panels": {}})
    display.process([])

    time.sleep(0.05)
    _, body = _get(f"http://localhost:{port}/api/status")
    panel = json.loads(body)["panels"]["empty_chain"]
    assert panel["count"] == 0
    assert panel["aircraft"] == []


# ===========================================================================
# 4. render_aircraft_dict
# ===========================================================================

def test_render_uses_registration():
    d = render_aircraft_dict(_make_aircraft(registration="G-ABCD"))
    assert d["ident"] == "G-ABCD"


def test_render_falls_back_to_callsign():
    d = render_aircraft_dict(_make_aircraft(hex_id="AA1111", callsign="BAW123"))
    assert d["ident"] == "BAW123"


def test_render_falls_back_to_icao_hex():
    d = render_aircraft_dict(_make_aircraft(hex_id="AA1111"))
    assert d["ident"] == "AA1111"


def test_render_aircraft_type():
    d = render_aircraft_dict(_make_aircraft(aircraft_type="BOEING 737-800"))
    assert d["aircraft_type"] == "BOEING 737-800"


def test_render_altitude_ground():
    d = render_aircraft_dict(_make_aircraft(altitude_feet=0))
    assert d["altitude"] == "GND"


def test_render_altitude_formatted():
    d = render_aircraft_dict(_make_aircraft(altitude_feet=35000))
    assert d["altitude"] == "35,000 ft"


def test_render_altitude_unknown():
    d = render_aircraft_dict(_make_aircraft(altitude_feet=None))
    assert d["altitude"] == "—"


def test_render_distance_formatted():
    d = render_aircraft_dict(_make_aircraft(distance_nm=12.345))
    assert d["distance"] == "12.3 nm"


def test_render_distance_with_cardinal():
    d = render_aircraft_dict(_make_aircraft(distance_nm=5.0, bearing_degrees=45.0))
    assert d["distance"] == "5.0 nm NE"


def test_render_distance_unknown():
    d = render_aircraft_dict(_make_aircraft(distance_nm=None))
    assert d["distance"] == "—"


def test_render_operator_present():
    d = render_aircraft_dict(_make_aircraft(operator="British Airways"))
    assert d["operator"] == "British Airways"


def test_render_operator_absent_is_null():
    d = render_aircraft_dict(_make_aircraft(operator=None))
    assert d["operator"] is None


def test_render_climbing():
    d = render_aircraft_dict(_make_aircraft(vertical_rate_fpm=512))
    assert d["vrate"] == "↑"


def test_render_descending():
    d = render_aircraft_dict(_make_aircraft(vertical_rate_fpm=-512))
    assert d["vrate"] == "↓"


def test_render_level():
    d = render_aircraft_dict(_make_aircraft(vertical_rate_fpm=0))
    assert d["vrate"] == "—"


def test_render_has_timestamp():
    d = render_aircraft_dict(_make_aircraft())
    assert "UTC" in d["timestamp"]


def test_render_airline_present():
    d = render_aircraft_dict(_make_aircraft(airline_name="Ryanair"))
    assert d["airline"] == "Ryanair"


def test_render_airline_absent_is_null():
    d = render_aircraft_dict(_make_aircraft(airline_name=None))
    assert d["airline"] is None


def test_render_route_both_iata():
    d = render_aircraft_dict(_make_aircraft(origin_iata="REU", destination_iata="LBA"))
    assert d["route"] == "REU → LBA"


def test_render_route_origin_only():
    d = render_aircraft_dict(_make_aircraft(origin_iata="REU"))
    assert d["route"] == "REU → ?"


def test_render_route_destination_only():
    d = render_aircraft_dict(_make_aircraft(destination_iata="LBA"))
    assert d["route"] == "? → LBA"


def test_render_route_neither_is_null():
    d = render_aircraft_dict(_make_aircraft())
    assert d["route"] is None
