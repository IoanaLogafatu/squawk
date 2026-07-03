"""
tests/test_schema_aircraft.py

Tests for schemas/aircraft.py.

Covers:
  1. New fields on AircraftRoute default to UNKNOWN
  2. New field on Airframe defaults to UNKNOWN
  3. New adsbdb field on AircraftRaw defaults to empty dict
  4. aircraft_from_dict round-trips all new fields via SquawkEncoder
  5. aircraft_from_dict backward-compat: old snapshots without new fields
     still deserialise without error and new fields default correctly
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from schemas.aircraft import (
    Aircraft, AircraftLocation, AircraftMeta, AircraftRaw,
    AircraftRoute, AircraftVector, Airframe, aircraft_from_dict,
)
from schemas.encoder import SquawkEncoder


FIXTURES = Path(__file__).parent / "fixtures"


def _bare_aircraft() -> Aircraft:
    return Aircraft(
        meta      = AircraftMeta(icao_hex="AABBCC", ingestor="test"),
        location  = AircraftLocation(),
        direction = AircraftVector(),
        route     = AircraftRoute(),
        airframe  = Airframe(),
        raw       = AircraftRaw(),
    )


# ---------------------------------------------------------------------------
# 1 & 2 & 3 — default values
# ---------------------------------------------------------------------------

def test_new_route_fields_default_to_unknown():
    a = _bare_aircraft()
    assert a.route.origin_name is None
    assert a.route.origin_country is None
    assert a.route.destination_name is None
    assert a.route.destination_country is None
    assert a.route.airline_name is None
    assert a.route.airline_country is None


def test_new_airframe_manufacturer_defaults_to_unknown():
    assert _bare_aircraft().airframe.manufacturer is None


def test_raw_adsbdb_defaults_to_empty_dict():
    assert _bare_aircraft().raw.adsbdb == {}


# ---------------------------------------------------------------------------
# 4 — round-trip via encoder
# ---------------------------------------------------------------------------

def test_aircraft_from_dict_round_trips_new_fields():
    a = Aircraft(
        meta      = AircraftMeta(
            icao_hex       = "4D2387",
            ingestor       = "test",
            observed_at    = datetime(2026, 5, 13, 17, 2, 11, tzinfo=timezone.utc),
            reception_type = "mlat",
        ),
        location  = AircraftLocation(),
        direction = AircraftVector(),
        route     = AircraftRoute(
            callsign            = "RYR54NN",
            origin_name         = "Reus Airport",
            origin_country      = "Spain",
            destination_name    = "Leeds Bradford Airport",
            destination_country = "United Kingdom",
            airline_name        = "Ryanair",
            airline_country     = "Ireland",
        ),
        airframe  = Airframe(
            manufacturer  = "Boeing",
            registration  = "9H-VUZ",
        ),
        raw       = AircraftRaw(
            adsbdb = {"aircraft": {"type": "B38M"}},
        ),
    )
    d = json.loads(json.dumps(a, cls=SquawkEncoder))
    r = aircraft_from_dict(d)

    assert r.route.origin_name         == "Reus Airport"
    assert r.route.origin_country      == "Spain"
    assert r.route.destination_name    == "Leeds Bradford Airport"
    assert r.route.destination_country == "United Kingdom"
    assert r.route.airline_name        == "Ryanair"
    assert r.route.airline_country     == "Ireland"
    assert r.airframe.manufacturer     == "Boeing"
    assert r.airframe.registration     == "9H-VUZ"
    assert r.raw.adsbdb                == {"aircraft": {"type": "B38M"}}


# ---------------------------------------------------------------------------
# 5 — backward-compat with pre-existing on-disk snapshots
# ---------------------------------------------------------------------------

def test_aircraft_from_dict_backward_compat_old_snapshot():
    d = json.loads((FIXTURES / "4D2387.json").read_text())
    a = aircraft_from_dict(d)

    assert a.airframe.manufacturer     is None
    assert a.route.origin_name         is None
    assert a.route.origin_country      is None
    assert a.route.destination_name    is None
    assert a.route.destination_country is None
    assert a.route.airline_name        is None
    assert a.route.airline_country     is None
    assert a.raw.adsbdb                == {}
