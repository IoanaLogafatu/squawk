"""
tests/test_band_closest.py

Tests for the band_closest selector module.

Covers:
  1. One aircraft per band — four returned, in D, C, B, A order
  2. Several aircraft in one band — lowest distance_nm wins
  3. An empty band is absent; the rest still descend
  4. UNKNOWN altitude_band excluded, even with a valid distance
  5. UNKNOWN distance_nm excluded, even with a valid band
  6. Empty input returns an empty list
  7. Returned aircraft keep their band — the module selects, it does not enrich
  8. A tie on distance_nm returns exactly one aircraft
  9. Band letters beyond D sort correctly — nothing assumes four bands
 10. Factory pooling — one [modules.band_closest] block, one instance
"""

from __future__ import annotations

import pytest

from modules import clear_module_pool, get_module
from modules.band_closest import BandClosest
from schemas.aircraft import (
    Aircraft, AircraftLocation, AircraftMeta, AircraftRaw,
    AircraftRoute, AircraftVector, Airframe,
)


@pytest.fixture(autouse=True)
def _reset_module_pool():
    clear_module_pool()
    yield
    clear_module_pool()


def _make_aircraft(hex_id: str, band, distance) -> Aircraft:
    return Aircraft(
        meta      = AircraftMeta(icao_hex=hex_id, ingestor="test"),
        location  = AircraftLocation(altitude_band=band, distance_nm=distance),
        direction = AircraftVector(),
        route     = AircraftRoute(),
        airframe  = Airframe(),
        raw       = AircraftRaw(),
    )


def _bands(result) -> list[str]:
    return [a.location.altitude_band for a in result]


def _hexes(result) -> list[str]:
    return [a.meta.icao_hex for a in result]


# ---------------------------------------------------------------------------
# 1. One per band, highest band first
# ---------------------------------------------------------------------------

def test_one_aircraft_per_band_returns_descending():
    aircraft = [
        _make_aircraft("AAAA01", "B", 12.0),
        _make_aircraft("AAAA02", "D", 40.0),
        _make_aircraft("AAAA03", "A", 3.0),
        _make_aircraft("AAAA04", "C", 25.0),
    ]
    result = BandClosest().process(aircraft)
    assert _bands(result) == ["D", "C", "B", "A"]
    assert _hexes(result) == ["AAAA02", "AAAA04", "AAAA01", "AAAA03"]


# ---------------------------------------------------------------------------
# 2. Nearest within a band wins
# ---------------------------------------------------------------------------

def test_nearest_in_band_wins():
    aircraft = [
        _make_aircraft("FAR", "C", 40.0),
        _make_aircraft("NEAR", "C", 4.0),
        _make_aircraft("MID", "C", 18.0),
    ]
    result = BandClosest().process(aircraft)
    assert _hexes(result) == ["NEAR"]


# ---------------------------------------------------------------------------
# 3. Empty bands contribute nothing
# ---------------------------------------------------------------------------

def test_empty_band_is_absent_and_order_holds():
    # Nothing in C: the output is three entries, not four with a gap.
    aircraft = [
        _make_aircraft("AAAA01", "A", 5.0),
        _make_aircraft("AAAA02", "B", 9.0),
        _make_aircraft("AAAA03", "D", 33.0),
    ]
    result = BandClosest().process(aircraft)
    assert _bands(result) == ["D", "B", "A"]


# ---------------------------------------------------------------------------
# 4 & 5. Candidacy rules
# ---------------------------------------------------------------------------

def test_unknown_band_excluded():
    aircraft = [
        _make_aircraft("NOBAND", None, 1.0),   # nearest of the two, still excluded
        _make_aircraft("BANDED", "B", 20.0),
    ]
    result = BandClosest().process(aircraft)
    assert _hexes(result) == ["BANDED"]


def test_unknown_distance_excluded():
    aircraft = [
        _make_aircraft("NODIST", "C", None),
        _make_aircraft("HASDIST", "C", 30.0),
    ]
    result = BandClosest().process(aircraft)
    assert _hexes(result) == ["HASDIST"]


def test_band_of_only_distanceless_aircraft_yields_nothing():
    aircraft = [
        _make_aircraft("NODIST1", "D", None),
        _make_aircraft("NODIST2", "D", None),
        _make_aircraft("AAAA01",  "A", 6.0),
    ]
    result = BandClosest().process(aircraft)
    assert _bands(result) == ["A"]


# ---------------------------------------------------------------------------
# 6. Empty input
# ---------------------------------------------------------------------------

def test_empty_list_returns_empty():
    assert BandClosest().process([]) == []


def test_no_candidates_returns_empty():
    assert BandClosest().process([_make_aircraft("NOBAND", None, None)]) == []


# ---------------------------------------------------------------------------
# 7. Selection only — records come back untouched
# ---------------------------------------------------------------------------

def test_returned_aircraft_keep_their_band():
    original = _make_aircraft("AAAA01", "C", 12.0)
    result = BandClosest().process([original])
    assert result[0] is original
    assert result[0].location.altitude_band == "C"
    assert result[0].location.distance_nm   == 12.0


# ---------------------------------------------------------------------------
# 8. Ties
# ---------------------------------------------------------------------------

def test_tie_on_distance_returns_exactly_one():
    aircraft = [
        _make_aircraft("AAAA01", "B", 15.0),
        _make_aircraft("AAAA02", "B", 15.0),
    ]
    result = BandClosest().process(aircraft)
    # Which one is unspecified; that there is one is not.
    assert len(result) == 1
    assert _bands(result) == ["B"]


# ---------------------------------------------------------------------------
# 9. Nothing assumes four bands
# ---------------------------------------------------------------------------

def test_bands_beyond_d_sort_correctly():
    aircraft = [
        _make_aircraft(letter, letter, 10.0 + i)
        for i, letter in enumerate(["A", "B", "C", "D", "E", "F"])
    ]
    result = BandClosest().process(aircraft)
    assert _bands(result) == ["F", "E", "D", "C", "B", "A"]


# ---------------------------------------------------------------------------
# 10. Factory pooling
# ---------------------------------------------------------------------------

def test_same_block_yields_one_instance():
    first  = get_module("band_closest")
    second = get_module("band_closest")
    assert first is second
