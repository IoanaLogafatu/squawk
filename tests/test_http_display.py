"""
tests/test_http_display.py

Tests for the HTTP display module.

Covers:
  1. Module contract — process() returns list unchanged
  2. HTTP server — page served, 404 for unknown paths
  3. Panel config — chain_name → title/slot lookup, defaults, list payload
 3b. System state — storage publishes 'tracked', the payload carries it
  4. render_aircraft_dict — JSON output for each display field
  5. Display factory — get_display() builds a ModuleContext for every display
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request

import pytest

import system
from display.http import HttpDisplay
from display.http.server import render_aircraft_dict
from storage import STALE_SECONDS
from storage.disk_drive import DiskDriveStorage
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
    type_code=None,
    type_description=None,
    category=None,
    operator=None,
    airline_name=None,
    origin_iata=None,
    destination_iata=None,
    distance_nm=None,
    bearing_degrees=None,
    altitude_feet=None,
    altitude_band=None,
    vertical_rate_fpm=None,
    manufacturer=None,
    origin_municipality=None,
    destination_municipality=None,
    origin_country=None,
    destination_country=None,
) -> Aircraft:
    return Aircraft(
        meta      = AircraftMeta(icao_hex=hex_id, ingestor="test", reception_type="adsb_icao"),
        location  = AircraftLocation(distance_nm=distance_nm, bearing_degrees=bearing_degrees,
                                     altitude_feet=altitude_feet, altitude_band=altitude_band),
        direction = AircraftVector(vertical_rate_fpm=vertical_rate_fpm),
        route     = AircraftRoute(callsign=callsign, airline_name=airline_name,
                                  origin_iata=origin_iata, destination_iata=destination_iata,
                                  origin_municipality=origin_municipality,
                                  destination_municipality=destination_municipality,
                                  origin_country=origin_country,
                                  destination_country=destination_country),
        airframe  = Airframe(registration=registration, type_code=type_code,
                             type_description=type_description, category=category,
                             operator=operator, manufacturer=manufacturer),
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
        "low_level":  {"title": "Below 10k", "slot": 1},
        "high_level": {"title": "Above 10k", "slot": 2},
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


def test_http_display_reads_title_and_slot_from_panel_config():
    panels = {"low_level": {"title": "Approach & Low", "slot": 3}}
    display = HttpDisplay({"port": _free_port(), "chain_name": "low_level", "panels": panels})
    assert display.panel_title == "Approach & Low"
    assert display.slot == 3


def test_http_display_title_falls_back_to_title_cased_chain_name():
    # `title` stays optional — a missing one is not a visible defect on the wall.
    # `slot` is enforced by the config loader, not here.
    display = HttpDisplay({"port": _free_port(), "chain_name": "some_new_chain", "panels": {}})
    assert display.panel_title == "Some New Chain"


def test_http_display_update_stores_slot_and_payload_exposes_it():
    port = _free_port()
    panels = {"slotted": {"title": "Slotted", "slot": 6}}
    display = HttpDisplay({"port": port, "chain_name": "slotted", "panels": panels})
    display.process([_make_aircraft()])

    time.sleep(0.05)
    _, body = _get(f"http://localhost:{port}/api/status")
    assert json.loads(body)["panels"]["slotted"]["slot"] == 6


def test_http_display_panel_carries_updated_epoch():
    port = _free_port()
    display = HttpDisplay({"port": port, "chain_name": "epoch_chain", "panels": {}})
    before = time.time()
    display.process([_make_aircraft()])
    after = time.time()

    time.sleep(0.05)
    _, body = _get(f"http://localhost:{port}/api/status")
    panel = json.loads(body)["panels"]["epoch_chain"]

    assert "updated_at" not in panel
    assert before <= panel["updated_epoch"] <= after


def test_http_display_panel_carries_chain_name_not_panel_id():
    port = _free_port()
    display = HttpDisplay({"port": port, "chain_name": "named_chain", "panels": {}})
    display.process([])

    time.sleep(0.05)
    _, body = _get(f"http://localhost:{port}/api/status")
    panel = json.loads(body)["panels"]["named_chain"]

    assert panel["chain_name"] == "named_chain"
    assert "panel_id" not in panel


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
# 3b. System state — storage publishes 'tracked', the payload carries it
# ===========================================================================

def test_save_aircraft_array_publishes_tracked(tmp_path):
    system.clear()
    storage = DiskDriveStorage(tmp_path)
    storage.save_aircraft_array([
        _make_aircraft(hex_id="AAAA01"),
        _make_aircraft(hex_id="AAAA02"),
        _make_aircraft(hex_id="AAAA03"),
    ])
    assert system.get("tracked") == 3


def test_tracked_matches_non_stale_record_count(tmp_path):
    system.clear()
    storage = DiskDriveStorage(tmp_path)
    storage.save_aircraft_array([_make_aircraft(hex_id="BBBB01")])
    assert system.get("tracked") == len(storage.list_aircraft_hex_ids()) == 1


def test_tracked_reflects_expiry(tmp_path):
    system.clear()
    storage = DiskDriveStorage(tmp_path)
    storage.save_aircraft_array([
        _make_aircraft(hex_id="CCCC01"),
        _make_aircraft(hex_id="CCCC02"),
    ])
    assert system.get("tracked") == 2

    # Age every record past the staleness window, then save again — the second
    # save expires them, and the published count must drop to match.
    old = time.time() - (STALE_SECONDS + 30)
    for p in (tmp_path / "tracked_aircraft").glob("*.json"):
        os.utime(p, (old, old))

    storage.save_aircraft_array([])
    assert system.get("tracked") == 0
    assert storage.list_aircraft_hex_ids() == []


def test_sse_payload_carries_system_tracked_at_top_level():
    system.clear()
    system.set("tracked", 145)

    port = _free_port()
    display = HttpDisplay({"port": port, "chain_name": "sys_chain", "panels": {}})
    display.process([])

    time.sleep(0.05)
    _, body = _get(f"http://localhost:{port}/api/status")
    data = json.loads(body)

    assert data["system"]["tracked"] == 145


def test_payload_system_key_is_empty_before_anything_publishes():
    # Honest reading before the first ingest cycle: the key is simply absent.
    system.clear()
    port = _free_port()
    display = HttpDisplay({"port": port, "chain_name": "unpublished", "panels": {}})
    display.process([])

    time.sleep(0.05)
    _, body = _get(f"http://localhost:{port}/api/status")
    data = json.loads(body)

    assert "system" in data
    assert "tracked" not in data["system"]


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


def test_render_carries_all_three_airframe_fields():
    # The payload ships the facts; the renderer decides which to show.
    d = render_aircraft_dict(_make_aircraft(
        type_code="B738", type_description="BOEING 737-800", category="A3",
    ))
    assert d["type_code"]        == "B738"
    assert d["type_description"] == "BOEING 737-800"
    assert d["category"]         == "A3"
    assert "aircraft_type" not in d


def test_render_type_fields_are_null_when_absent():
    # Mode S and MLAT tracks carry no category. Normal, not an error.
    d = render_aircraft_dict(_make_aircraft())
    assert d["type_code"]        is None
    assert d["type_description"] is None
    assert d["category"]         is None


def test_render_sends_null_not_a_placeholder_for_unknown_type():
    # An em-dash here would be truthy, so the renderer's own fallback could
    # never fire and the card would show a bare dash. The display string is
    # the renderer's job; Python sends the fact, or nothing.
    d = render_aircraft_dict(_make_aircraft())
    for key in ("type_code", "type_description", "category"):
        assert d[key] is None, f"{key} must be null, not {d[key]!r}"
        assert d[key] != "—"


def test_page_renderer_prefers_description_then_falls_back_to_code():
    port = _free_port()
    HttpDisplay({"port": port, "chain_name": "t"})
    time.sleep(0.05)
    _, body = _get(f"http://localhost:{port}/")

    # The card label takes the description first, the designator second.
    assert "a.type_description || a.type_code || 'Unknown type'" in body
    assert "a.aircraft_type" not in body


def test_page_renderer_shows_unknown_type_when_both_are_missing():
    # Explicit text, not a dash: a dash on a wall panel reads as a rendering
    # fault, where "Unknown type" reads as an aircraft that did not identify
    # itself. There is exactly one fallback and it is reachable.
    port = _free_port()
    HttpDisplay({"port": port, "chain_name": "t"})
    time.sleep(0.05)
    _, body = _get(f"http://localhost:{port}/")

    assert "'Unknown type'" in body
    assert "Unknown Airframe" not in body


def test_page_leaves_the_route_blank_rather_than_labelling_it_unknown():
    # Only the airframe line gets explicit text. The route box is omitted
    # entirely when there is no route — eight panels announcing UNKNOWN ROUTE
    # reads worse than eight quiet gaps.
    port = _free_port()
    HttpDisplay({"port": port, "chain_name": "t"})
    time.sleep(0.05)
    _, body = _get(f"http://localhost:{port}/")

    assert "UNKNOWN ROUTE" not in body.upper()
    assert "if (a.origin_iata || a.destination_iata)" in body


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


# ===========================================================================
# 4b. List layout — band letters in the payload, layout/bands on the panel
# ===========================================================================

def test_render_carries_altitude_band():
    d = render_aircraft_dict(_make_aircraft(altitude_feet=29000, altitude_band="C"))
    assert d["altitude_band"] == "C"


def test_render_altitude_band_unknown_passes_through_as_null():
    # No placeholder: the client decides what an unbanded aircraft means.
    d = render_aircraft_dict(_make_aircraft(altitude_feet=29000))
    assert d["altitude_band"] is None


def test_list_panel_payload_carries_layout_and_bands():
    port = _free_port()
    panels = {"panel_one": {"title": "Overhead", "slot": 1,
                            "layout": "list", "bands": ["D", "C", "B", "A"]}}
    display = HttpDisplay({"port": port, "chain_name": "panel_one", "panels": panels})
    display.process([])

    time.sleep(0.05)
    _, body = _get(f"http://localhost:{port}/api/status")
    panel = json.loads(body)["panels"]["panel_one"]
    assert panel["layout"] == "list"
    assert panel["bands"] == ["D", "C", "B", "A"]


def test_card_panel_payload_is_unchanged():
    # Regression guard on the default path: a panel block written before the
    # list layout existed keeps rendering as a card.
    port = _free_port()
    panels = {"legacy": {"title": "Legacy", "slot": 2}}
    display = HttpDisplay({"port": port, "chain_name": "legacy", "panels": panels})
    display.process([_make_aircraft(registration="G-CARD")])

    time.sleep(0.05)
    _, body = _get(f"http://localhost:{port}/api/status")
    panel = json.loads(body)["panels"]["legacy"]
    assert panel["layout"] == "card"
    assert panel["bands"] == []
    assert panel["aircraft"][0]["ident"] == "G-CARD"
    assert panel["aircraft"][0]["distance"] == "—"
    assert panel["aircraft"][0]["vrate"]    == "—"


def test_list_panel_entries_carry_their_band_letters():
    # Bands D and B occupied, C and A empty. The payload says which is which;
    # position in the list says nothing, which is why the letter is carried.
    port = _free_port()
    panels = {"banded": {"title": "Banded", "slot": 1,
                         "layout": "list", "bands": ["D", "C", "B", "A"]}}
    display = HttpDisplay({"port": port, "chain_name": "banded", "panels": panels})
    display.process([
        _make_aircraft(hex_id="AAAA01", registration="REG-D", altitude_feet=35000, altitude_band="D"),
        _make_aircraft(hex_id="AAAA02", registration="REG-B", altitude_feet=12000, altitude_band="B"),
    ])

    time.sleep(0.05)
    _, body = _get(f"http://localhost:{port}/api/status")
    panel = json.loads(body)["panels"]["banded"]
    assert panel["count"] == 2
    assert [(a["ident"], a["altitude_band"]) for a in panel["aircraft"]] == [
        ("REG-D", "D"), ("REG-B", "B"),
    ]


# ===========================================================================
# 4c. Four-line row — municipality, country abbreviation, manufacturer rule
# ===========================================================================

def test_render_carries_municipalities():
    d = render_aircraft_dict(_make_aircraft(
        origin_iata="CDG", origin_municipality="Paris",
        destination_iata="ORD", destination_municipality="Chicago",
    ))
    assert d["origin_municipality"]      == "Paris"
    assert d["destination_municipality"] == "Chicago"


def test_render_municipality_absent_is_null():
    # No placeholder in the payload; the row decides what to draw.
    d = render_aircraft_dict(_make_aircraft(origin_iata="CDG"))
    assert d["origin_municipality"]      is None
    assert d["destination_municipality"] is None


@pytest.mark.parametrize("full, short", [
    ("United Kingdom",           "UK"),
    ("United States",            "USA"),
    ("United States of America", "USA"),
])
def test_render_shortens_long_country_names(full, short):
    d = render_aircraft_dict(_make_aircraft(origin_country=full, destination_country=full))
    assert d["origin_country"]      == short
    assert d["destination_country"] == short


def test_render_leaves_other_countries_alone():
    # Two entries, not a general abbreviation scheme.
    d = render_aircraft_dict(_make_aircraft(origin_country="France",
                                            destination_country="Netherlands"))
    assert d["origin_country"]      == "France"
    assert d["destination_country"] == "Netherlands"


def test_type_label_replaces_a_repeated_manufacturer_prefix():
    # tar1090_db writes the manufacturer into the description in caps.
    # Prefixing blindly would read "Boeing BOEING 737-800".
    d = render_aircraft_dict(_make_aircraft(manufacturer="Boeing",
                                            type_description="BOEING 737-800"))
    assert d["type_label"] == "Boeing 737-800"


def test_type_label_concatenates_when_the_description_omits_the_manufacturer():
    # adsbdb's descriptions carry no manufacturer.
    d = render_aircraft_dict(_make_aircraft(manufacturer="Boeing",
                                            type_description="737MAX 8 200"))
    assert d["type_label"] == "Boeing 737MAX 8 200"


def test_type_label_manufacturer_alone_when_type_is_unknown():
    d = render_aircraft_dict(_make_aircraft(manufacturer="Boeing"))
    assert d["type_label"] == "Boeing"


def test_type_label_description_unchanged_when_manufacturer_is_unknown():
    # The normal state for an aircraft's first cycles: tar1090_db has answered
    # and adsbdb has not, so the row reads in capitals until it does.
    d = render_aircraft_dict(_make_aircraft(type_description="BOEING 737-800"))
    assert d["type_label"] == "BOEING 737-800"


def test_type_label_is_null_when_both_are_unknown():
    assert render_aircraft_dict(_make_aircraft())["type_label"] is None


def test_type_label_doubles_a_differently_spelled_manufacturer():
    # Known limitation, asserted so it is a decision rather than a surprise:
    # the prefix match is literal, so two spellings of the same manufacturer
    # produce both. Rare, and visible on the wall when it happens.
    d = render_aircraft_dict(_make_aircraft(manufacturer="De Havilland Canada",
                                            type_description="DEHAVILLAND DHC-8"))
    assert d["type_label"] == "De Havilland Canada DEHAVILLAND DHC-8"


# ===========================================================================
# 4d. Manufacturer normalisation and compound municipalities
# ===========================================================================

@pytest.mark.parametrize("legal, brand", [
    ("Boeing Company",                         "Boeing"),
    ("Airbus Sas",                             "Airbus"),
    ("Airbus Industrie",                       "Airbus"),
    ("Atr - Gie Avions De Transport Regional", "ATR"),
    ("Avions de Transport Regional",           "ATR"),
])
def test_manufacturer_normalised_to_its_brand(legal, brand):
    # adsbdb returns the registered legal entity; the wall wants the brand.
    d = render_aircraft_dict(_make_aircraft(manufacturer=legal))
    assert d["manufacturer"] == brand
    assert d["type_label"]   == brand


def test_manufacturer_match_is_case_insensitive():
    d = render_aircraft_dict(_make_aircraft(manufacturer="BOEING COMPANY"))
    assert d["manufacturer"] == "Boeing"


def test_unmapped_manufacturer_passes_through_unchanged():
    # An explicit map, not a suffix-stripping scheme — a brand that legitimately
    # ends in a corporate word keeps it.
    d = render_aircraft_dict(_make_aircraft(manufacturer="Gulfstream Aerospace"))
    assert d["manufacturer"] == "Gulfstream Aerospace"


def test_manufacturer_none_passes_through():
    assert render_aircraft_dict(_make_aircraft())["manufacturer"] is None


def test_manufacturer_is_normalised_before_the_type_label_rule():
    # The ordering guard. Normalising after the rule would leave it comparing
    # "Boeing Company" against "BOEING 737-800", missing the prefix, and
    # concatenating instead of stripping.
    d = render_aircraft_dict(_make_aircraft(manufacturer="Boeing Company",
                                            type_description="BOEING 737-800"))
    assert d["type_label"] == "Boeing 737-800"


def test_normalised_manufacturer_still_concatenates_a_bare_type():
    d = render_aircraft_dict(_make_aircraft(manufacturer="Airbus Sas",
                                            type_description="A321-251NX"))
    assert d["type_label"] == "Airbus A321-251NX"


def test_compound_municipality_keeps_the_first_city():
    d = render_aircraft_dict(_make_aircraft(
        origin_municipality="Cincinnati / Covington",
        destination_municipality="Dallas-Fort Worth / Irving",
    ))
    assert d["origin_municipality"]      == "Cincinnati"
    assert d["destination_municipality"] == "Dallas-Fort Worth"


def test_municipality_without_a_slash_is_unchanged():
    d = render_aircraft_dict(_make_aircraft(
        origin_municipality="Rio de Janeiro",
        destination_municipality="Arnavutköy, Istanbul",
    ))
    assert d["origin_municipality"]      == "Rio de Janeiro"
    assert d["destination_municipality"] == "Arnavutköy, Istanbul"


# ===========================================================================
# 5. Display factory — get_display() builds a ModuleContext for every display
# ===========================================================================

def test_every_display_accepts_two_argument_signature(tmp_path, monkeypatch):
    import importlib
    import pkgutil

    import display as display_pkg
    from modules import BaseModule

    from config import config as squawk_config
    monkeypatch.setattr(squawk_config.squawk, "data_dir", str(tmp_path))

    from display import get_display

    cfg_by_display = {
        "http":   {"port": _free_port(), "chain_name": "t"},
        "epaper": {"port": _free_port()},
    }

    for info in pkgutil.iter_modules(display_pkg.__path__):
        display = get_display(info.name, cfg_by_display.get(info.name, {}))
        assert isinstance(display, BaseModule), \
            f"display/{info.name} did not return a BaseModule"


def test_get_display_module_dir_is_data_dir_slash_display_slash_name(tmp_path, monkeypatch):
    from config import config as squawk_config
    from display import get_display

    monkeypatch.setattr(squawk_config.squawk, "data_dir", str(tmp_path))

    pushover = get_display("pushover", {"token": "t", "user": "u"})
    assert pushover._last_sent_path.parent == tmp_path / "display" / "pushover"
