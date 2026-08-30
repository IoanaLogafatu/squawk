"""
tests/test_modules.py

Tests for processor modules.

Covers:
  1. ClosestFilter — selects nearest, excludes unknowns, handles empty list
  2. EpaperDisplay — renders to image, skips redraw when data unchanged, handles empty list
  3. ConsoleDisplay — prints single-line output
  4. Module factory pooling — get_module() shares instances by (name, cfg)
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image
from unittest.mock import MagicMock

import modules
from config import ObserverConfig
from modules import ModuleContext, clear_module_pool, get_module
from modules.closest_filter import ClosestFilter
from display.console import ConsoleDisplay
from display.epaper import EpaperDisplay
from display.epaper.renderer import render, WIDTH, HEIGHT
from schemas.aircraft import (
    Aircraft, AircraftLocation, AircraftMeta, AircraftRaw,
    AircraftRoute, AircraftVector, Airframe,
)


# ---------------------------------------------------------------------------
# Keep the module factory pool isolated between tests in this file
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_module_pool():
    clear_module_pool()
    yield
    clear_module_pool()


def _ctx(tmp_path: Path) -> ModuleContext:
    return ModuleContext(
        data_dir=tmp_path,
        module_dir=tmp_path / "display" / "epaper",
        observer=ObserverConfig(latitude=53.7778, longitude=-1.5721),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_aircraft(hex_id: str, distance_nm=None, registration=None, aircraft_type=None, callsign=None) -> Aircraft:
    return Aircraft(
        meta      = AircraftMeta(icao_hex=hex_id, ingestor="test", reception_type="adsb_icao"),
        location  = AircraftLocation(distance_nm=distance_nm),
        direction = AircraftVector(),
        route     = AircraftRoute(callsign=callsign),
        airframe  = Airframe(registration=registration, aircraft_type=aircraft_type),
        raw       = AircraftRaw(),
    )



# ===========================================================================
# 1. ClosestFilter
# ===========================================================================

def test_closest_filter_returns_single_aircraft():
    aircraft = [
        _make_aircraft("AA1111", distance_nm=10.0),
        _make_aircraft("BB2222", distance_nm=3.5),
        _make_aircraft("CC3333", distance_nm=15.0),
    ]
    result = ClosestFilter().process(aircraft)
    assert len(result) == 1
    assert result[0].meta.icao_hex == "BB2222"


def test_closest_filter_excludes_no_distance():
    # Aircraft without distance_nm cannot be ranked — excluded from candidates.
    aircraft = [
        _make_aircraft("AA1111", distance_nm=None),
        _make_aircraft("BB2222", distance_nm=5.0),
    ]
    result = ClosestFilter().process(aircraft)
    assert len(result) == 1
    assert result[0].meta.icao_hex == "BB2222"


def test_closest_filter_all_no_distance_returns_empty():
    aircraft = [
        _make_aircraft("AA1111", distance_nm=None),
        _make_aircraft("BB2222", distance_nm=None),
    ]
    assert ClosestFilter().process(aircraft) == []


def test_closest_filter_empty_list_returns_empty():
    assert ClosestFilter().process([]) == []


def test_closest_filter_single_aircraft_returned():
    aircraft = [_make_aircraft("AA1111", distance_nm=7.2)]
    result = ClosestFilter().process(aircraft)
    assert len(result) == 1
    assert result[0].meta.icao_hex == "AA1111"


# ===========================================================================
# 2. EpaperDisplay
# ===========================================================================

def test_epaper_display_renders_aircraft(tmp_path):
    display = EpaperDisplay({"port": 0}, _ctx(tmp_path))
    display.process([_make_aircraft("AA1111", distance_nm=5.0)])
    png = tmp_path / "display" / "epaper" / "squawk_display.png"
    assert png.exists()
    img = Image.open(png)
    assert img.size == (WIDTH, HEIGHT)
    assert img.mode == "1"


def test_epaper_display_only_redraws_on_change(tmp_path):
    display = EpaperDisplay({"port": 0}, _ctx(tmp_path))
    mock_output = MagicMock()
    display._output = mock_output

    a1 = _make_aircraft("AA1111", distance_nm=5.0, registration="G-AAAA")
    a2 = _make_aircraft("BB2222", distance_nm=5.0, registration="G-BBBB")

    display.process([a1])   # first render — signature was None
    display.process([a1])   # same data — no redraw
    display.process([a2])   # different aircraft — redraw

    assert mock_output.write.call_count == 2


def test_epaper_display_handles_empty_list(tmp_path):
    display = EpaperDisplay({"port": 0}, _ctx(tmp_path))
    display.process([])     # empty list → "no aircraft" image
    png = tmp_path / "display" / "epaper" / "squawk_display.png"
    assert png.exists()


def test_epaper_display_no_redraw_when_still_empty(tmp_path):
    display = EpaperDisplay({"port": 0}, _ctx(tmp_path))
    mock_output = MagicMock()
    display._output = mock_output

    display.process([])
    display.process([])

    assert mock_output.write.call_count == 1


def test_render_returns_correct_image_size():
    img = render(_make_aircraft("AA1111"))
    assert img.size == (WIDTH, HEIGHT)


def test_render_none_returns_image():
    img = render(None)
    assert isinstance(img, Image.Image)
    assert img.size == (WIDTH, HEIGHT)


def test_epaper_format_country_and_route():
    from display.epaper.renderer import _format_country, _route_str

    assert _format_country("United Kingdom") == "UK"
    assert _format_country("united kingdom") == "UK"
    assert _format_country("uk") == "UK"
    assert _format_country("Germany") == "Germany"

    a = _make_aircraft("AA1111", callsign="EZY18ZQ")
    a.route.origin_iata = "NRN"
    a.route.origin_country = "Germany"
    a.route.destination_iata = "EDI"
    a.route.destination_country = "United Kingdom"

    route_str = _route_str(a)
    assert route_str == "NRN (Germany)  →  EDI (UK)"


def test_epaper_line_2_format():
    a = _make_aircraft("AA1111", registration="G-EZOK", aircraft_type="Airbus A320", callsign="EZY18ZQ")
    callsign = (a.route.callsign or "").strip().upper()
    typ = a.airframe.aircraft_type or ""
    cs_str = f"[{callsign}] " if callsign else ""
    typ_line = f"{cs_str}{typ}".strip()
    assert typ_line == "[EZY18ZQ] Airbus A320"



# ===========================================================================
# 3. ConsoleDisplay
# ===========================================================================

def test_console_display_prints_registration_and_type(capsys):
    a = _make_aircraft("AA1111", registration="G-TEST", aircraft_type="A320")
    ConsoleDisplay().process([a])
    out = capsys.readouterr().out
    assert "G-TEST" in out
    assert "A320" in out


def test_console_display_unknown_fields_show_placeholder(capsys):
    ConsoleDisplay().process([_make_aircraft("AA1111")])
    out = capsys.readouterr().out
    assert "???" in out


def test_console_display_empty_list_prints_no_aircraft(capsys):
    ConsoleDisplay().process([])
    out = capsys.readouterr().out
    assert "no aircraft" in out


def test_console_display_returns_list_unchanged():
    aircraft = [_make_aircraft("AA1111"), _make_aircraft("BB2222")]
    result = ConsoleDisplay().process(aircraft)
    assert result is aircraft


# ===========================================================================
# 4. Module factory pooling
# ===========================================================================

def test_same_name_same_config_one_instance():
    first  = get_module("closest_filter")
    second = get_module("closest_filter")
    assert first is second


def test_same_type_different_blocks_are_distinct_instances():
    low = get_module("low_altitude", {"type": "altitude_filter", "below": 5000})
    mid = get_module("mid_altitude", {"type": "altitude_filter", "above": 5000, "below": 15000})
    assert low is not mid
    assert low._below == 5000
    assert mid._above == 5000


def test_different_types_no_config_are_distinct_instances():
    closest = get_module("closest_filter")
    passthru = get_module("pass_through")
    assert closest is not passthru


def test_list_valued_config_key_round_trips():
    a = get_module("registration_filter", {"registrations": ["G-ABCD", "G-EFGH"]})
    b = get_module("registration_filter", {"registrations": ["G-ABCD", "G-EFGH"]})
    c = get_module("registration_filter", {"registrations": ["G-ZZZZ"]})
    assert a is b
    assert a is not c


def test_nested_config_key_is_stable_across_calls():
    cfg = {"type": "pass_through", "extra": {"a": 1, "b": [1, 2, 3]}}
    a = get_module("weird_alias", cfg)
    b = get_module("weird_alias", dict(cfg))   # separate dict, same content
    c = get_module("weird_alias", {"type": "pass_through", "extra": {"a": 1, "b": [1, 2, 4]}})
    assert a is b
    assert a is not c


def test_unknown_module_raises_and_leaves_no_pool_entry():
    with pytest.raises(ValueError):
        get_module("nonexistent")
    assert not any(key[0] == "nonexistent" for key in modules._INSTANCES)


def test_clear_module_pool_produces_fresh_instances():
    first = get_module("closest_filter")
    clear_module_pool()
    second = get_module("closest_filter")
    assert first is not second


# ===========================================================================
# 5. Module context — every module accepts ctx, module_dir is type-keyed
# ===========================================================================

def test_every_module_accepts_two_argument_signature(tmp_path, monkeypatch):
    import importlib
    import pkgutil

    from modules import tar1090_db as tar1090_db_module

    # Avoid a real network call from tar1090_db's first-run CSV download.
    monkeypatch.setattr(
        tar1090_db_module, "_download",
        lambda path: (_ for _ in ()).throw(RuntimeError("no network in tests")),
    )

    ctx = ModuleContext(
        data_dir=tmp_path,
        module_dir=tmp_path / "modules" / "generic",
        observer=ObserverConfig(latitude=53.7778, longitude=-1.5721),
    )

    for info in pkgutil.iter_modules(modules.__path__):
        mod = importlib.import_module(f"modules.{info.name}")
        instance = mod.get({}, ctx)
        assert isinstance(instance, modules.BaseModule), \
            f"modules/{info.name}.py's get() did not return a BaseModule"


def test_module_dir_keyed_on_type_not_block_name(tmp_path, monkeypatch):
    from config import config as squawk_config
    monkeypatch.setattr(squawk_config.squawk, "data_dir", str(tmp_path))

    enricher = get_module("adsbdb_tv", {"type": "adsbdb"})
    assert enricher._cache_dir == tmp_path / "modules" / "adsbdb"
    assert not (tmp_path / "modules" / "adsbdb_tv").exists()


def test_factory_does_not_create_module_dir(tmp_path, monkeypatch):
    from config import config as squawk_config
    monkeypatch.setattr(squawk_config.squawk, "data_dir", str(tmp_path))

    get_module("pass_through")
    assert not (tmp_path / "modules" / "pass_through").exists()
