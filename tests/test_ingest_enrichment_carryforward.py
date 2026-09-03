"""
tests/test_ingest_enrichment_carryforward.py

Tests for _carry_forward_enrichment (ingestor/personal_adsb/ingestor.py),
which fills a freshly-built Aircraft's UNKNOWN airframe/route fields from
the matching hex's record already in storage, so hex/callsign-keyed
enrichment modules (tar1090_db, vrs_route) don't repeat a lookup for an
aircraft that's already been enriched and is still in range.

Covers:
  1. First sighting of a hex — no-op, full enrichment runs
  2. Same hex, same callsign, second cycle — fields carry forward, and the
     enrichment module's own lookup is not called again (call count, not
     just field values)
  3. Same hex, different callsign — route does not carry forward and the
     module runs again for it; airframe still carries forward
  4. Hex present but past STALE_SECONDS — treated as absent
  5. Raw ADS-B supplies airframe.registration this cycle — raw wins over a
     different carried-forward value
  6. UNKNOWN in both fresh and stored — stays UNKNOWN
  7. callsign None on both sides — treated as a match, route carries forward
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest

from data_sources.vrs_standing_data import SQLiteVrsDb
from ingestor.personal_adsb.ingestor import _carry_forward_enrichment
from modules.tar1090_db import Tar1090DbEnricher
from modules.vrs_route import VrsRouteEnricher
from schemas.aircraft import (
    Aircraft, AircraftLocation, AircraftMeta, AircraftRaw,
    AircraftRoute, AircraftVector, Airframe,
)
from storage import STALE_SECONDS
from storage.disk_drive import DiskDriveStorage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_aircraft(hex_id="4CA068", callsign=None, **overrides) -> Aircraft:
    """A freshly-converted-looking Aircraft: meta/location/direction filled,
    route.callsign/squawk_code as given, everything else UNKNOWN unless
    overridden — the shape _build_aircraft hands to carry-forward each cycle.
    """
    route_kwargs = {k: v for k, v in overrides.items() if k in _ROUTE_FIELDS}
    airframe_kwargs = {k: v for k, v in overrides.items() if k in _AIRFRAME_FIELDS}
    return Aircraft(
        meta      = AircraftMeta(icao_hex=hex_id, ingestor="test", reception_type="adsb_icao"),
        location  = AircraftLocation(latitude=1.0, longitude=2.0),
        direction = AircraftVector(),
        route     = AircraftRoute(callsign=callsign, **route_kwargs),
        airframe  = Airframe(**airframe_kwargs),
        raw       = AircraftRaw(),
    )


_ROUTE_FIELDS = {
    "squawk_code", "origin_iata", "origin_name", "origin_municipality", "origin_country",
    "destination_iata", "destination_name", "destination_municipality", "destination_country",
    "flight_number", "airline_name", "airline_country",
}
_AIRFRAME_FIELDS = {
    "registration", "type_code", "type_description", "category",
    "db_flags", "manufacturer", "operator",
}


def _seed(storage: DiskDriveStorage, aircraft: Aircraft, age_seconds: float = 0.0) -> None:
    """Write `aircraft` to storage as the 'existing' record from a prior cycle."""
    storage.save_aircraft_array([aircraft])
    if age_seconds:
        path = storage.aircraft_dir / f"{aircraft.meta.icao_hex}.json"
        old = time.time() - age_seconds
        os.utime(path, (old, old))


_FULL_AIRFRAME = dict(
    registration="G-EUPT", type_code="A320", type_description="AIRBUS A-320",
    category="A3", db_flags=0, manufacturer="Airbus", operator="Malta Air",
)
_FULL_ROUTE = dict(
    origin_iata="LHR", origin_name="Heathrow Airport", origin_municipality="London",
    origin_country="United Kingdom", destination_iata="JFK",
    destination_name="John F Kennedy International Airport",
    destination_municipality="New York", destination_country="United States",
    flight_number="BA117", airline_name="British Airways", airline_country="United Kingdom",
)


@pytest.fixture
def storage(tmp_path) -> DiskDriveStorage:
    return DiskDriveStorage(tmp_path)


# ===========================================================================
# 1. First sighting — no-op
# ===========================================================================

def test_first_sighting_is_a_noop(storage):
    fresh = _make_aircraft(callsign="BAW117")
    result = _carry_forward_enrichment([fresh], storage)
    assert result[0] is fresh
    assert result[0].airframe.registration is None
    assert result[0].route.origin_iata is None


# ===========================================================================
# 2. Same hex, same callsign — fields carry forward, module skips its lookup
# ===========================================================================

def test_same_hex_same_callsign_carries_forward_airframe_and_route(storage):
    existing = _make_aircraft(callsign="BAW117", **_FULL_AIRFRAME, **_FULL_ROUTE)
    _seed(storage, existing)

    fresh = _make_aircraft(callsign="BAW117")
    result = _carry_forward_enrichment([fresh], storage)

    af = result[0].airframe
    assert af.registration == "G-EUPT"
    assert af.type_code == "A320"
    assert af.type_description == "AIRBUS A-320"
    assert af.category == "A3"
    assert af.db_flags == 0
    assert af.manufacturer == "Airbus"
    assert af.operator == "Malta Air"

    rt = result[0].route
    for field, value in _FULL_ROUTE.items():
        assert getattr(rt, field) == value


class _CountingDict:
    """dict.get() with a call counter, standing in for Tar1090DbEnricher's db."""

    def __init__(self, data: dict) -> None:
        self._data = data
        self.calls = 0

    def get(self, key):
        self.calls += 1
        return self._data.get(key)


_VRS_AIRPORTS = (
    # code, name, icao, iata, location, country_iso2, lat, lon, alt
    ("LHR", "Heathrow Airport", "EGLL", "LHR", "London", "GB", 51.47, -0.45, 83),
    ("JFK", "John F Kennedy International Airport", "KJFK", "JFK", "New York", "US", 40.64, -73.78, 13),
    ("LIS", "Humberto Delgado Airport", "LPPT", "LIS", "Lisbon", "PT", 38.77, -9.13, 374),
    ("BCN", "Josep Tarradellas Barcelona-El Prat Airport", "LEBL", "BCN", "Barcelona", "ES", 41.29, 2.07, 12),
)
_VRS_COUNTRIES = (
    ("GB", "United Kingdom"), ("US", "United States"), ("PT", "Portugal"), ("ES", "Spain"),
)
_VRS_AIRLINES = (
    ("BAW", "British Airways", "BAW", "BA", None, None),
    ("TAP", "TAP Air Portugal", "TAP", "TP", None, None),
)


def _build_vrs_db(tmp_path: Path, routes: tuple) -> Path:
    db_path = tmp_path / "vrs.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE routes (callsign TEXT, code TEXT, number INTEGER, "
        "airline_code TEXT, airport_codes TEXT)"
    )
    conn.executemany("INSERT INTO routes VALUES (?,?,?,?,?)", routes)
    conn.execute(
        "CREATE TABLE airports (code TEXT, name TEXT, icao TEXT, iata TEXT, "
        "location TEXT, country_iso2 TEXT, latitude REAL, longitude REAL, altitude_feet INTEGER)"
    )
    conn.executemany("INSERT INTO airports VALUES (?,?,?,?,?,?,?,?,?)", _VRS_AIRPORTS)
    conn.execute("CREATE TABLE countries (iso TEXT PRIMARY KEY, name TEXT)")
    conn.executemany("INSERT INTO countries VALUES (?,?)", _VRS_COUNTRIES)
    conn.execute(
        "CREATE TABLE airlines (code TEXT, name TEXT, icao TEXT, iata TEXT, "
        "positioning_flight_pattern TEXT, charter_flight_pattern TEXT)"
    )
    conn.executemany("INSERT INTO airlines VALUES (?,?,?,?,?,?)", _VRS_AIRLINES)
    conn.commit()
    conn.close()
    return db_path


class _CountingVrsDb:
    """Wraps a real SQLiteVrsDb and counts get_route() calls."""

    def __init__(self, db_path: Path) -> None:
        self._inner = SQLiteVrsDb(db_path)
        self.calls = 0

    def get_route(self, callsign: str):
        self.calls += 1
        return self._inner.get_route(callsign)

    def get_airport(self, icao: str):
        return self._inner.get_airport(icao)

    def get_country(self, iso2: str):
        return self._inner.get_country(iso2)

    def get_airline(self, code: str):
        return self._inner.get_airline(code)


class _FakeVrsSource:
    def __init__(self, db) -> None:
        self._db = db

    def ensure_fresh(self) -> None:
        pass

    @property
    def db(self):
        return self._db


def test_second_cycle_same_callsign_does_not_repeat_the_lookup(storage, tmp_path):
    # Cycle 1: nothing in storage, both enrichers run and populate.
    tar_db = _CountingDict({"4CA068": ("EI-CJX", "B752", "BOEING 757-200", 0)})
    tar_enricher = Tar1090DbEnricher(db=tar_db)

    vrs_db_path = _build_vrs_db(tmp_path, routes=(("BAW117", "BA", 117, "BAW", "EGLL-KJFK"),))
    vrs_db = _CountingVrsDb(vrs_db_path)
    vrs_enricher = VrsRouteEnricher(source=_FakeVrsSource(vrs_db), log_dir=tmp_path / "log")

    cycle1 = _make_aircraft(hex_id="4CA068", callsign="BAW117")
    cycle1 = _carry_forward_enrichment([cycle1], storage)   # no-op, nothing stored yet
    cycle1 = tar_enricher.process(cycle1)
    cycle1 = vrs_enricher.process(cycle1)
    assert tar_db.calls == 1
    assert vrs_db.calls == 1
    assert cycle1[0].airframe.registration == "EI-CJX"
    assert cycle1[0].route.origin_iata == "LHR"

    storage.save_aircraft_array(cycle1)

    # Cycle 2: same hex, same callsign — carry-forward fills both airframe
    # and route before either module runs.
    cycle2 = _make_aircraft(hex_id="4CA068", callsign="BAW117")
    cycle2 = _carry_forward_enrichment([cycle2], storage)
    assert cycle2[0].airframe.registration == "EI-CJX"
    assert cycle2[0].route.origin_iata == "LHR"

    cycle2 = tar_enricher.process(cycle2)
    cycle2 = vrs_enricher.process(cycle2)

    # Both modules now gate their own lookup behind a _needs_*() pre-check
    # (vrs_route's _needs_route(), tar1090_db's _needs_airframe() — see
    # brief-tar1090-needs-check.md) — once carry-forward has filled every
    # field either gate checks, neither lookup happens a second time.
    assert vrs_db.calls == 1     # unchanged — the lookup that didn't happen
    assert tar_db.calls == 1     # unchanged — the lookup that didn't happen
    assert cycle2[0].airframe.registration == "EI-CJX"


# ===========================================================================
# 3. Same hex, different callsign — route does not carry forward
# ===========================================================================

def test_different_callsign_does_not_carry_forward_route(storage):
    existing = _make_aircraft(callsign="BAW117", **_FULL_AIRFRAME, **_FULL_ROUTE)
    _seed(storage, existing)

    fresh = _make_aircraft(callsign="TAP123")
    result = _carry_forward_enrichment([fresh], storage)

    # Route did not carry forward at all.
    rt = result[0].route
    assert rt.callsign == "TAP123"
    for field in _FULL_ROUTE:
        assert getattr(rt, field) is None

    # Airframe still carries forward — it's keyed on hex, not callsign.
    assert result[0].airframe.registration == "G-EUPT"


def test_different_callsign_module_runs_again_for_the_new_route(storage, tmp_path):
    existing = _make_aircraft(hex_id="4CA068", callsign="BAW117", **_FULL_ROUTE)
    _seed(storage, existing)

    vrs_db_path = _build_vrs_db(tmp_path, routes=(
        ("BAW117", "BA", 117, "BAW", "EGLL-KJFK"),
        ("TAP123", "TP", 123, "TAP", "LPPT-LEBL"),
    ))
    vrs_db = _CountingVrsDb(vrs_db_path)
    vrs_enricher = VrsRouteEnricher(source=_FakeVrsSource(vrs_db), log_dir=tmp_path / "log")

    fresh = _make_aircraft(hex_id="4CA068", callsign="TAP123")
    fresh = _carry_forward_enrichment([fresh], storage)
    assert fresh[0].route.origin_iata is None   # not carried forward

    fresh = vrs_enricher.process(fresh)
    assert vrs_db.calls == 1                    # ran again for the new callsign
    assert fresh[0].route.origin_iata == "LIS"   # LPPT resolved fresh


# ===========================================================================
# 4. Past STALE_SECONDS — treated as absent
# ===========================================================================

def test_stale_record_is_treated_as_absent(storage):
    existing = _make_aircraft(callsign="BAW117", **_FULL_AIRFRAME, **_FULL_ROUTE)
    _seed(storage, existing, age_seconds=STALE_SECONDS + 5)

    fresh = _make_aircraft(callsign="BAW117")
    result = _carry_forward_enrichment([fresh], storage)

    assert result[0].airframe.registration is None
    assert result[0].route.origin_iata is None


# ===========================================================================
# 5. Raw ADS-B this cycle wins over a different carried-forward value
# ===========================================================================

def test_raw_registration_this_cycle_wins_over_stored_value(storage):
    existing = _make_aircraft(callsign="BAW117", registration="G-OLD")
    _seed(storage, existing)

    fresh = _make_aircraft(callsign="BAW117", registration="G-NEW")   # this cycle's raw 'r' field
    result = _carry_forward_enrichment([fresh], storage)

    assert result[0].airframe.registration == "G-NEW"


# ===========================================================================
# 6. UNKNOWN in both — stays UNKNOWN, merging doesn't invent data
# ===========================================================================

def test_unknown_in_both_stays_unknown(storage):
    existing = _make_aircraft(callsign="BAW117")   # nothing enriched yet
    _seed(storage, existing)

    fresh = _make_aircraft(callsign="BAW117")
    result = _carry_forward_enrichment([fresh], storage)

    assert result[0].airframe.registration is None
    assert result[0].route.origin_iata is None


# ===========================================================================
# 7. callsign None on both sides — treated as a match
# ===========================================================================

def test_no_callsign_on_either_side_still_carries_forward_route(storage):
    existing = _make_aircraft(callsign=None, **_FULL_ROUTE)   # e.g. airline_name somehow known
    _seed(storage, existing)

    fresh = _make_aircraft(callsign=None)
    result = _carry_forward_enrichment([fresh], storage)

    assert result[0].route.callsign is None
    assert result[0].route.airline_name == _FULL_ROUTE["airline_name"]
    assert result[0].route.origin_iata == _FULL_ROUTE["origin_iata"]


# ===========================================================================
# meta/location/direction are never touched, and neither is callsign/squawk
# ===========================================================================

def test_meta_location_direction_and_callsign_are_never_overwritten(storage):
    existing = _make_aircraft(
        hex_id="4CA068", callsign="OLD999", squawk_code="7000", **_FULL_AIRFRAME,
    )
    _seed(storage, existing)

    fresh = Aircraft(
        meta      = AircraftMeta(icao_hex="4CA068", ingestor="test", reception_type="mlat"),
        location  = AircraftLocation(latitude=9.0, longitude=9.0),
        direction = AircraftVector(ground_speed_knots=123.0),
        route     = AircraftRoute(callsign="NEW111", squawk_code="7500"),
        airframe  = Airframe(),
        raw       = AircraftRaw(),
    )
    result = _carry_forward_enrichment([fresh], storage)

    assert result[0].meta.reception_type == "mlat"
    assert result[0].location.latitude == 9.0
    assert result[0].direction.ground_speed_knots == 123.0
    assert result[0].route.callsign == "NEW111"
    assert result[0].route.squawk_code == "7500"
    # airframe still carries forward independently of the callsign change —
    # it's hex-keyed, not callsign-keyed.
    assert result[0].airframe.registration == "G-EUPT"
