"""
tests/test_tar1090_db.py

Tests for the tar1090_db enricher module.

Covers:
  1. Fills registration when UNKNOWN
  2. Fills type_code and type_description independently
  3. Does not overwrite existing registration
  4. Does not overwrite an existing type_code or type_description
  5. Unknown ICAO hex leaves record unchanged
  6. Empty aircraft list is handled
  7. get() loads the real CSV without error
"""

from __future__ import annotations

import csv
import pytest

from modules.tar1090_db import Tar1090DbEnricher, _load_db
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
    type_code=None,
    type_description=None,
) -> Aircraft:
    return Aircraft(
        meta      = AircraftMeta(icao_hex=hex_id, ingestor="test", reception_type="adsb_icao"),
        location  = AircraftLocation(),
        direction = AircraftVector(),
        route     = AircraftRoute(),
        airframe  = Airframe(registration=registration, type_code=type_code,
                             type_description=type_description),
        raw       = AircraftRaw(),
    )


def _make_enricher(entries: dict[str, tuple]) -> Tar1090DbEnricher:
    return Tar1090DbEnricher(db=entries)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_fills_registration_when_unknown():
    enricher = _make_enricher({"4CA068": ("EI-CJX", "B752", "BOEING 757-200")})
    aircraft = [_make_aircraft("4CA068")]
    result = enricher.process(aircraft)
    assert result[0].airframe.registration == "EI-CJX"


def test_fills_type_code_and_description_independently():
    # Both CSV columns are carried through. They used to be collapsed into one
    # value that preferred the description, discarding the designator.
    enricher = _make_enricher({"4CA068": ("EI-CJX", "B752", "BOEING 757-200")})
    result = enricher.process([_make_aircraft("4CA068")])
    assert result[0].airframe.type_code        == "B752"
    assert result[0].airframe.type_description == "BOEING 757-200"


def test_row_with_no_description_still_yields_a_type_code():
    enricher = _make_enricher({"004002": ("Z-WPA", "B732", None)})
    result = enricher.process([_make_aircraft("004002")])
    assert result[0].airframe.type_code        == "B732"
    assert result[0].airframe.type_description is None


def test_does_not_overwrite_existing_registration():
    enricher = _make_enricher({"4CA068": ("EI-CJX", "B752", "BOEING 757-200")})
    aircraft = [_make_aircraft("4CA068", registration="G-KEEP")]
    result = enricher.process(aircraft)
    assert result[0].airframe.registration == "G-KEEP"


def test_does_not_overwrite_existing_type_code():
    enricher = _make_enricher({"4CA068": ("EI-CJX", "B752", "BOEING 757-200")})
    result = enricher.process([_make_aircraft("4CA068", type_code="A320")])
    assert result[0].airframe.type_code == "A320"


def test_does_not_overwrite_existing_type_description():
    enricher = _make_enricher({"4CA068": ("EI-CJX", "B752", "BOEING 757-200")})
    result = enricher.process([_make_aircraft("4CA068", type_description="Airbus A320")])
    assert result[0].airframe.type_description == "Airbus A320"


def test_fills_the_missing_half_only():
    # A record that already has a code but no description gets the description
    # filled without its code being touched.
    enricher = _make_enricher({"4CA068": ("EI-CJX", "B752", "BOEING 757-200")})
    result = enricher.process([_make_aircraft("4CA068", type_code="B757")])
    assert result[0].airframe.type_code        == "B757"
    assert result[0].airframe.type_description == "BOEING 757-200"


def test_unknown_hex_leaves_record_unchanged():
    enricher = _make_enricher({"4CA068": ("EI-CJX", "B752", "BOEING 757-200")})
    aircraft = [_make_aircraft("FFFFFF")]
    result = enricher.process(aircraft)
    assert result[0].airframe.registration     is None
    assert result[0].airframe.type_code        is None
    assert result[0].airframe.type_description is None


def test_empty_list_returns_empty():
    enricher = _make_enricher({"4CA068": ("EI-CJX", "B752", "BOEING 757-200")})
    assert enricher.process([]) == []


def test_returns_same_list_object():
    enricher = _make_enricher({})
    aircraft = [_make_aircraft("AA1111")]
    result = enricher.process(aircraft)
    assert result is aircraft


def test_load_db_parses_csv(tmp_path):
    csv_file = tmp_path / "aircraft.csv"
    csv_file.write_text(
        "4CA068;EI-CJX;B752;00;BOEING 757-200;;;\n"   # both columns present
        "004002;Z-WPA;B732;00;;;;\n"                   # type code, no description
        "AABBCC;;;00;;;;\n",                            # empty registration and type
        encoding="utf-8",
    )
    db = _load_db(csv_file)
    assert db["4CA068"] == ("EI-CJX", "B752", "BOEING 757-200")
    assert db["004002"] == ("Z-WPA", "B732", None)
    assert db["AABBCC"] == (None, None, None)


def test_load_db_normalises_hex_to_uppercase(tmp_path):
    csv_file = tmp_path / "aircraft.csv"
    csv_file.write_text("4ca068;EI-CJX;B752;00;;;;\n", encoding="utf-8")
    db = _load_db(csv_file)
    assert "4CA068" in db
    assert "4ca068" not in db


def test_missing_csv_returns_noop_enricher(tmp_path, monkeypatch):
    from config import ObserverConfig
    from modules import ModuleContext, tar1090_db

    monkeypatch.setattr(tar1090_db, "_download", lambda path: (_ for _ in ()).throw(RuntimeError("no network")))
    ctx = ModuleContext(
        data_dir=tmp_path,
        module_dir=tmp_path / "modules" / "tar1090_db",
        observer=ObserverConfig(latitude=53.7778, longitude=-1.5721),
    )
    enricher = tar1090_db.get({}, ctx)
    aircraft = [_make_aircraft("4CA068")]
    result = enricher.process(aircraft)
    assert result[0].airframe.registration     is None
    assert result[0].airframe.type_code        is None
    assert result[0].airframe.type_description is None
