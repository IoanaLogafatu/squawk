"""
tests/test_modules.py

Tests for processor modules.

Covers:
  1. ClosestFilter — selects nearest, excludes unknowns, handles empty list
  2. EpaperDisplay — renders to image, skips redraw when data unchanged, handles empty list
  3. ConsoleDisplay — prints single-line output
"""

from __future__ import annotations

import pytest
from PIL import Image
from unittest.mock import MagicMock

from modules.closest_filter import ClosestFilter
from display.console import ConsoleDisplay
from display.epaper import EpaperDisplay
from display.epaper.renderer import render, WIDTH, HEIGHT
from schemas.aircraft import (
    Aircraft, AircraftLocation, AircraftMeta, AircraftRaw,
    AircraftRoute, AircraftVector, Airframe,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_aircraft(hex_id: str, distance_nm=None, registration=None, aircraft_type=None) -> Aircraft:
    return Aircraft(
        meta      = AircraftMeta(icao_hex=hex_id, ingestor="test", reception_type="adsb_icao"),
        location  = AircraftLocation(distance_nm=distance_nm),
        direction = AircraftVector(),
        route     = AircraftRoute(),
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
    display = EpaperDisplay({"data_dir": str(tmp_path), "port": 0})
    display.process([_make_aircraft("AA1111", distance_nm=5.0)])
    png = tmp_path / "display" / "epaper" / "squawk_display.png"
    assert png.exists()
    img = Image.open(png)
    assert img.size == (WIDTH, HEIGHT)
    assert img.mode == "1"


def test_epaper_display_only_redraws_on_change(tmp_path):
    display = EpaperDisplay({"data_dir": str(tmp_path), "port": 0})
    mock_output = MagicMock()
    display._output = mock_output

    a1 = _make_aircraft("AA1111", distance_nm=5.0, registration="G-AAAA")
    a2 = _make_aircraft("BB2222", distance_nm=5.0, registration="G-BBBB")

    display.process([a1])   # first render — signature was None
    display.process([a1])   # same data — no redraw
    display.process([a2])   # different aircraft — redraw

    assert mock_output.write.call_count == 2


def test_epaper_display_handles_empty_list(tmp_path):
    display = EpaperDisplay({"data_dir": str(tmp_path), "port": 0})
    display.process([])     # empty list → "no aircraft" image
    png = tmp_path / "display" / "epaper" / "squawk_display.png"
    assert png.exists()


def test_epaper_display_no_redraw_when_still_empty(tmp_path):
    display = EpaperDisplay({"data_dir": str(tmp_path), "port": 0})
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
