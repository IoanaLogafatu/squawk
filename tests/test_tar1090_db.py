"""
tests/test_tar1090_db.py

Tests for the tar1090_db enricher plugin.

Covers:
  1. Fills registration when UNKNOWN
  2. Fills aircraft_type when UNKNOWN
  3. Does not overwrite existing registration
  4. Does not overwrite existing aircraft_type
  5. Unknown ICAO hex leaves record unchanged
  6. Empty aircraft list is handled
  7. get() loads the real CSV without error
"""

from __future__ import annotations

import csv
import pytest

from plugins.tar1090_db import Tar1090DbEnricher, _load_db
from schemas.aircraft import (
    Aircraft, AircraftLocation, AircraftMeta, AircraftRaw,
    AircraftRoute, AircraftVector, Airframe,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_aircraft(
    hex_id: str,
    registration=None,
    aircraft_type=None,
) -> Aircraft:
    return Aircraft(
        meta      = AircraftMeta(icao_hex=hex_id, ingestor="test", reception_type="adsb_icao"),
        location  = AircraftLocation(),
        direction = AircraftVector(),
        route     = AircraftRoute(),
        airframe  = Airframe(registration=registration, aircraft_type=aircraft_type),
        raw       = AircraftRaw(),
    )


def _make_enricher(entries: dict[str, tuple]) -> Tar1090DbEnricher:
    return Tar1090DbEnricher(db=entries)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_fills_registration_when_unknown():
    enricher = _make_enricher({"4CA068": ("EI-CJX", "B752")})
    aircraft = [_make_aircraft("4CA068")]
    result = enricher.process(aircraft)
    assert result[0].airframe.registration == "EI-CJX"


def test_fills_aircraft_type_when_unknown():
    enricher = _make_enricher({"4CA068": ("EI-CJX", "BOEING 757-200")})
    aircraft = [_make_aircraft("4CA068")]
    result = enricher.process(aircraft)
    assert result[0].airframe.aircraft_type == "BOEING 757-200"


def test_does_not_overwrite_existing_registration():
    enricher = _make_enricher({"4CA068": ("EI-CJX", "B752")})
    aircraft = [_make_aircraft("4CA068", registration="G-KEEP")]
    result = enricher.process(aircraft)
    assert result[0].airframe.registration == "G-KEEP"


def test_does_not_overwrite_existing_aircraft_type():
    enricher = _make_enricher({"4CA068": ("EI-CJX", "B752")})
    aircraft = [_make_aircraft("4CA068", aircraft_type="A320")]
    result = enricher.process(aircraft)
    assert result[0].airframe.aircraft_type == "A320"


def test_unknown_hex_leaves_record_unchanged():
    enricher = _make_enricher({"4CA068": ("EI-CJX", "B752")})
    aircraft = [_make_aircraft("FFFFFF")]
    result = enricher.process(aircraft)
    assert result[0].airframe.registration is None
    assert result[0].airframe.aircraft_type is None


def test_empty_list_returns_empty():
    enricher = _make_enricher({"4CA068": ("EI-CJX", "B752")})
    assert enricher.process([]) == []


def test_returns_same_list_object():
    enricher = _make_enricher({})
    aircraft = [_make_aircraft("AA1111")]
    result = enricher.process(aircraft)
    assert result is aircraft


def test_load_db_parses_csv(tmp_path):
    csv_file = tmp_path / "aircraft.csv"
    csv_file.write_text(
        "4CA068;EI-CJX;B752;00;BOEING 757-200;;;\n"   # description present → use it
        "004002;Z-WPA;B732;00;;;;\n"                   # no description → fall back to type code
        "AABBCC;;;00;;;;\n",                            # empty registration and type
        encoding="utf-8",
    )
    db = _load_db(csv_file)
    assert db["4CA068"] == ("EI-CJX", "BOEING 757-200")
    assert db["004002"] == ("Z-WPA", "B732")
    assert db["AABBCC"] == (None, None)


def test_load_db_normalises_hex_to_uppercase(tmp_path):
    csv_file = tmp_path / "aircraft.csv"
    csv_file.write_text("4ca068;EI-CJX;B752;00;;;;\n", encoding="utf-8")
    db = _load_db(csv_file)
    assert "4CA068" in db
    assert "4ca068" not in db


def test_missing_csv_returns_noop_enricher(tmp_path, monkeypatch):
    from plugins import tar1090_db
    monkeypatch.setattr(tar1090_db, "_download", lambda path: (_ for _ in ()).throw(RuntimeError("no network")))
    enricher = tar1090_db.get({"csv_path": str(tmp_path / "nonexistent.csv")})
    aircraft = [_make_aircraft("4CA068")]
    result = enricher.process(aircraft)
    assert result[0].airframe.registration is None
    assert result[0].airframe.aircraft_type is None
