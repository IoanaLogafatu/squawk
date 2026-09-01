"""
tests/test_altitude_band.py

Tests for the altitude_band enrichment module.

Covers:
  1. Each band assigned for a mid-band altitude
  2. Boundary values land in the band above — the whole reason this module
     exists rather than four altitude_filter blocks
  3. Ground (0 ft) is band A
  4. Unknown altitude leaves the band UNKNOWN, no exception
  5. A single edge gives two bands
  6. Config validation rejects unusable edge lists
  7. Storage round-trip preserves the band
  8. Factory pooling — one [modules.altitude_band] block, one instance
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from modules import clear_module_pool, get_module
from modules.altitude_band import AltitudeBand
from schemas.aircraft import (
    Aircraft, AircraftLocation, AircraftMeta, AircraftRaw,
    AircraftRoute, AircraftVector, Airframe,
)
from storage.disk_drive import DiskDriveStorage


EDGES = [10000, 20000, 30000]


@pytest.fixture(autouse=True)
def _reset_module_pool():
    clear_module_pool()
    yield
    clear_module_pool()


def _make_aircraft(altitude, hex_id: str = "AABBCC") -> Aircraft:
    return Aircraft(
        meta      = AircraftMeta(
            icao_hex    = hex_id,
            ingestor    = "test",
            observed_at = datetime.now(timezone.utc),
        ),
        location  = AircraftLocation(altitude_feet=altitude, seen_seconds=0.0),
        direction = AircraftVector(),
        route     = AircraftRoute(),
        airframe  = Airframe(),
        raw       = AircraftRaw(),
    )


def _band(altitude, edges=EDGES):
    return AltitudeBand(edges).process([_make_aircraft(altitude)])[0].location.altitude_band


# ---------------------------------------------------------------------------
# 1. Mid-band altitudes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("altitude, expected", [
    (5000,  "A"),
    (15000, "B"),
    (25000, "C"),
    (38000, "D"),
])
def test_mid_band_altitude_assigned(altitude, expected):
    assert _band(altitude) == expected


# ---------------------------------------------------------------------------
# 2. Boundaries — half-open upward
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("altitude, expected", [
    (9999,  "A"),
    (10000, "B"),
    (19999, "B"),
    (20000, "C"),
    (29999, "C"),
    (30000, "D"),
])
def test_boundary_altitude_belongs_to_the_band_above(altitude, expected):
    assert _band(altitude) == expected


# ---------------------------------------------------------------------------
# 3 & 4. Ground and unknown altitude
# ---------------------------------------------------------------------------

def test_ground_is_band_a():
    # altitude_feet is already normalised: "ground" arrives here as 0.
    assert _band(0) == "A"


def test_unknown_altitude_leaves_band_unknown():
    assert _band(None) is None


def test_empty_aircraft_list_is_handled():
    assert AltitudeBand(EDGES).process([]) == []


# ---------------------------------------------------------------------------
# 5. Letters come from the edge count, not a hard-coded four
# ---------------------------------------------------------------------------

def test_single_edge_gives_two_bands():
    assert _band(4000,  edges=[5000]) == "A"
    assert _band(5000,  edges=[5000]) == "B"
    assert _band(41000, edges=[5000]) == "B"


def test_twenty_five_edges_give_twenty_six_bands():
    edges = [(i + 1) * 1000 for i in range(25)]
    assert _band(500, edges=edges)    == "A"
    assert _band(25000, edges=edges)  == "Z"


# ---------------------------------------------------------------------------
# 6. Config validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("edges", [
    None,                                  # missing — no default
    [],                                    # empty
    [30000, 20000, 10000],                 # descending
    [10000, 10000, 20000],                 # duplicate
    [10050],                               # not a multiple of 100
    [-10000],                              # negative
    [0],                                   # not positive
    ["10000"],                             # not a number
    [(i + 1) * 1000 for i in range(26)],   # 26 edges — letters stop being letters
])
def test_rejects_unusable_edges(edges):
    with pytest.raises(ValueError):
        AltitudeBand(edges)


def test_factory_requires_edges():
    with pytest.raises(ValueError):
        get_module("altitude_band", {})


# ---------------------------------------------------------------------------
# 7. Storage round-trip
# ---------------------------------------------------------------------------

def test_storage_round_trip_preserves_band(tmp_path):
    enriched = AltitudeBand(EDGES).process([_make_aircraft(24000, hex_id="4CA068")])

    backend = DiskDriveStorage(tmp_path)
    backend.save_aircraft_array(enriched)

    stored = backend.retrieve_aircraft("4CA068")
    assert stored["location"]["altitude_band"] == "C"

    restored = backend.retrieve_aircraft_objects()
    assert [a.location.altitude_band for a in restored] == ["C"]


# ---------------------------------------------------------------------------
# 8. Factory pooling
# ---------------------------------------------------------------------------

def test_same_block_yields_one_instance():
    first  = get_module("altitude_band", {"edges": EDGES})
    second = get_module("altitude_band", {"edges": EDGES})
    assert first is second


def test_different_edges_are_distinct_instances():
    a = get_module("altitude_band", {"edges": EDGES})
    b = get_module("altitude_band", {"edges": [5000, 15000]})
    assert a is not b
