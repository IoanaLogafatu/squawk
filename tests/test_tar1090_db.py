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
  10. process() re-checks CSV staleness hourly, not just at construction
  11. refresh_days is optional config, defaulting to 30
  12. _needs_airframe() pre-check — self._db.get() is skipped once every
      field this module could fill is already known (brief-tar1090-needs-check.md)
"""

from __future__ import annotations

import csv
import time

import pytest

from modules.tar1090_db import (
    Tar1090DbEnricher, _SCHEMA_VERSION, _build_sqlite_db, _db_schema_version,
    _load_db, _needs_airframe, _parse_db_flags,
)
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
    db_flags=None,
) -> Aircraft:
    return Aircraft(
        meta      = AircraftMeta(icao_hex=hex_id, ingestor="test", reception_type="adsb_icao"),
        location  = AircraftLocation(),
        direction = AircraftVector(),
        route     = AircraftRoute(),
        airframe  = Airframe(registration=registration, type_code=type_code,
                             type_description=type_description, db_flags=db_flags),
        raw       = AircraftRaw(),
    )


def _make_enricher(entries: dict[str, tuple]) -> Tar1090DbEnricher:
    return Tar1090DbEnricher(db=entries)


class _FakeTime:
    """Replaces the `time` module tar1090_db calls `time.time()` through.

    Patched in via `monkeypatch.setattr(tar1090_db, "time", _FakeTime(box))`
    — that only rebinds the name inside tar1090_db's own module namespace, so
    unrelated code (including pytest itself) keeps using the real clock.
    `box` is a one-element list so the test can advance it after the fact.
    """

    def __init__(self, box: list[float]) -> None:
        self._box = box

    def time(self) -> float:
        return self._box[0]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_fills_registration_when_unknown():
    enricher = _make_enricher({"4CA068": ("EI-CJX", "B752", "BOEING 757-200", 0)})
    aircraft = [_make_aircraft("4CA068")]
    result = enricher.process(aircraft)
    assert result[0].airframe.registration == "EI-CJX"


def test_fills_type_code_and_description_independently():
    # Both CSV columns are carried through. They used to be collapsed into one
    # value that preferred the description, discarding the designator.
    enricher = _make_enricher({"4CA068": ("EI-CJX", "B752", "BOEING 757-200", 0)})
    result = enricher.process([_make_aircraft("4CA068")])
    assert result[0].airframe.type_code        == "B752"
    assert result[0].airframe.type_description == "BOEING 757-200"


def test_row_with_no_description_still_yields_a_type_code():
    enricher = _make_enricher({"004002": ("Z-WPA", "B732", None, None)})
    result = enricher.process([_make_aircraft("004002")])
    assert result[0].airframe.type_code        == "B732"
    assert result[0].airframe.type_description is None


def test_does_not_overwrite_existing_registration():
    enricher = _make_enricher({"4CA068": ("EI-CJX", "B752", "BOEING 757-200", 0)})
    aircraft = [_make_aircraft("4CA068", registration="G-KEEP")]
    result = enricher.process(aircraft)
    assert result[0].airframe.registration == "G-KEEP"


def test_does_not_overwrite_existing_type_code():
    enricher = _make_enricher({"4CA068": ("EI-CJX", "B752", "BOEING 757-200", 0)})
    result = enricher.process([_make_aircraft("4CA068", type_code="A320")])
    assert result[0].airframe.type_code == "A320"


def test_does_not_overwrite_existing_type_description():
    enricher = _make_enricher({"4CA068": ("EI-CJX", "B752", "BOEING 757-200", 0)})
    result = enricher.process([_make_aircraft("4CA068", type_description="Airbus A320")])
    assert result[0].airframe.type_description == "Airbus A320"


def test_fills_the_missing_half_only():
    # A record that already has a code but no description gets the description
    # filled without its code being touched.
    enricher = _make_enricher({"4CA068": ("EI-CJX", "B752", "BOEING 757-200", 0)})
    result = enricher.process([_make_aircraft("4CA068", type_code="B757")])
    assert result[0].airframe.type_code        == "B757"
    assert result[0].airframe.type_description == "BOEING 757-200"


def test_unknown_hex_leaves_record_unchanged():
    enricher = _make_enricher({"4CA068": ("EI-CJX", "B752", "BOEING 757-200", 0)})
    aircraft = [_make_aircraft("FFFFFF")]
    result = enricher.process(aircraft)
    assert result[0].airframe.registration     is None
    assert result[0].airframe.type_code        is None
    assert result[0].airframe.type_description is None


def test_empty_list_returns_empty():
    enricher = _make_enricher({"4CA068": ("EI-CJX", "B752", "BOEING 757-200", 0)})
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
        "004002;Z-WPA;B732;10;;;;\n"                   # type code, no description, military
        "AABBCC;;;00;;;;\n",                            # empty registration and type
        encoding="utf-8",
    )
    db = _load_db(csv_file)
    assert db["4CA068"] == ("EI-CJX", "B752", "BOEING 757-200", 0)
    assert db["004002"] == ("Z-WPA", "B732", None, 1)    # '10' → bit 0 → military
    assert db["AABBCC"] == (None, None, None, 0)


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


# ---------------------------------------------------------------------------
# 8. dbFlags — the CSV column is a little-endian bit string
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("00",    0),    # no flags — a fact, not "unknown"
    ("10",    1),    # military
    ("01",    2),    # interesting
    ("11",    3),    # military | interesting
    ("0010",  4),    # PIA
    ("0001",  8),    # LADD
    ("1001",  9),    # military | LADD
    ("0101",  10),   # interesting | LADD
    ("11000", 3),    # military | interesting, trailing padding ignored
    ("0000",  0),
])
def test_parse_db_flags_reads_character_i_as_bit_i(raw, expected):
    # Character at index i is bit i. Reading the column as decimal or hex
    # yields plausible-looking wrong flags for every aircraft, which is worse
    # than having none: '0010' is PIA (4), not two or sixteen.
    assert _parse_db_flags(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "0x08", "8", "abc", "2"])
def test_parse_db_flags_returns_none_for_empty_or_malformed(raw):
    # None is "we don't know", which must not be confused with 0.
    assert _parse_db_flags(raw) is None


def test_parse_db_flags_distinguishes_zero_from_unknown():
    assert _parse_db_flags("00") == 0
    assert _parse_db_flags("00") is not None
    assert _parse_db_flags("") is None


def test_fills_db_flags_from_csv_column():
    enricher = _make_enricher({"4CA068": ("EI-CJX", "B752", "BOEING 757-200", 9)})
    result = enricher.process([_make_aircraft("4CA068")])
    assert result[0].airframe.db_flags == 9


def test_fills_db_flags_when_zero():
    # `is not None`, not truthiness: 0 must still be written.
    enricher = _make_enricher({"4CA068": ("EI-CJX", "B752", "BOEING 757-200", 0)})
    result = enricher.process([_make_aircraft("4CA068")])
    assert result[0].airframe.db_flags == 0


def test_does_not_overwrite_existing_db_flags():
    # A value from the receiver's own database wins over the CSV — it is what
    # that receiver actually used.
    enricher = _make_enricher({"4CA068": ("EI-CJX", "B752", "BOEING 757-200", 9)})
    result = enricher.process([_make_aircraft("4CA068", db_flags=4)])
    assert result[0].airframe.db_flags == 4


def test_does_not_overwrite_existing_db_flags_of_zero():
    enricher = _make_enricher({"4CA068": ("EI-CJX", "B752", "BOEING 757-200", 8)})
    result = enricher.process([_make_aircraft("4CA068", db_flags=0)])
    assert result[0].airframe.db_flags == 0


def test_db_flags_left_none_when_csv_has_none():
    enricher = _make_enricher({"4CA068": ("EI-CJX", "B752", "BOEING 757-200", None)})
    result = enricher.process([_make_aircraft("4CA068")])
    assert result[0].airframe.db_flags is None


# ---------------------------------------------------------------------------
# 9. Schema version — a stale index is rebuilt, a current one is not
# ---------------------------------------------------------------------------

def _build_index(tmp_path):
    csv_file = tmp_path / "aircraft.csv"
    csv_file.write_text("4CA068;EI-CJX;B752;0001;BOEING 757-200;;;\n", encoding="utf-8")
    db_file = tmp_path / "aircraft.db"
    _build_sqlite_db(csv_file, db_file)
    return csv_file, db_file


def test_built_index_carries_the_current_schema_version(tmp_path):
    _, db_file = _build_index(tmp_path)
    assert _db_schema_version(db_file) == _SCHEMA_VERSION == 3


def test_built_index_stores_parsed_flags(tmp_path):
    import sqlite3
    _, db_file = _build_index(tmp_path)
    conn = sqlite3.connect(str(db_file))
    row = conn.execute("SELECT reg, type_code, description, flags FROM aircraft").fetchone()
    conn.close()
    assert row == ("EI-CJX", "B752", "BOEING 757-200", 8)   # '0001' → LADD


def test_stale_schema_version_is_detected(tmp_path):
    # An index written by an older Squawk has a different column count and must
    # be discarded rather than read.
    import sqlite3
    _, db_file = _build_index(tmp_path)
    conn = sqlite3.connect(str(db_file))
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()

    assert _db_schema_version(db_file) == 2
    assert _db_schema_version(db_file) != _SCHEMA_VERSION


def test_missing_index_reports_version_zero(tmp_path):
    assert _db_schema_version(tmp_path / "absent.db") == 0


def test_get_rebuilds_a_version_2_index_without_touching_the_csv(tmp_path, monkeypatch):
    import os, sqlite3
    from config import ObserverConfig
    from modules import ModuleContext, tar1090_db

    module_dir = tmp_path / "modules" / "tar1090_db"
    module_dir.mkdir(parents=True)
    csv_file, db_file = _build_index(module_dir)

    # Downgrade the index in place and make it look older-but-not-stale-by-mtime,
    # so only the schema check can catch it.
    conn = sqlite3.connect(str(db_file))
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()
    newer = os.path.getmtime(csv_file) + 10
    os.utime(db_file, (newer, newer))

    monkeypatch.setattr(tar1090_db, "_download", lambda path: None)
    ctx = ModuleContext(
        data_dir=tmp_path,
        module_dir=module_dir,
        observer=ObserverConfig(latitude=53.7778, longitude=-1.5721),
    )
    enricher = tar1090_db.get({}, ctx)
    enricher.process([])   # first process() call triggers the refresh check

    assert _db_schema_version(db_file) == _SCHEMA_VERSION


def test_get_leaves_a_current_index_alone(tmp_path, monkeypatch):
    import os
    from config import ObserverConfig
    from modules import ModuleContext, tar1090_db

    module_dir = tmp_path / "modules" / "tar1090_db"
    module_dir.mkdir(parents=True)
    csv_file, db_file = _build_index(module_dir)
    newer = os.path.getmtime(csv_file) + 10
    os.utime(db_file, (newer, newer))
    before = os.path.getmtime(db_file)

    monkeypatch.setattr(tar1090_db, "_download", lambda path: None)
    monkeypatch.setattr(
        tar1090_db, "_build_sqlite_db",
        lambda c, d: pytest.fail("a current index must not be rebuilt"),
    )
    ctx = ModuleContext(
        data_dir=tmp_path,
        module_dir=module_dir,
        observer=ObserverConfig(latitude=53.7778, longitude=-1.5721),
    )
    enricher = tar1090_db.get({}, ctx)
    enricher.process([])   # first process() call triggers the refresh check

    assert os.path.getmtime(db_file) == before


# ---------------------------------------------------------------------------
# 10. Periodic refresh — the check must run from process(), not just once at
#     construction, or a long-lived process never notices the CSV going stale.
# ---------------------------------------------------------------------------

def test_first_process_call_triggers_a_refresh_check():
    # Construction no longer performs the check itself — the first process()
    # call must, regardless of _last_check's initial state.
    enricher = Tar1090DbEnricher(db={})
    calls = []
    enricher._maybe_refresh = lambda: calls.append(1)

    enricher.process([])

    assert calls == [1]


def test_second_call_within_the_hour_does_not_recheck(monkeypatch):
    from modules import tar1090_db

    t = [1_700_000_000.0]
    monkeypatch.setattr(tar1090_db, "time", _FakeTime(t))

    enricher = Tar1090DbEnricher(db={})
    calls = []
    enricher._maybe_refresh = lambda: calls.append(1)

    for _ in range(5):
        t[0] += 1   # several rapid process() calls, well inside the hour
        enricher.process([])

    assert len(calls) == 1


def test_call_after_the_interval_elapsed_rechecks(monkeypatch):
    from modules import tar1090_db
    from modules.tar1090_db import _CHECK_INTERVAL_SECONDS

    t = [1_700_000_000.0]
    monkeypatch.setattr(tar1090_db, "time", _FakeTime(t))

    enricher = Tar1090DbEnricher(db={})
    calls = []
    enricher._maybe_refresh = lambda: calls.append(1)

    enricher.process([])                       # first call
    t[0] += _CHECK_INTERVAL_SECONDS
    enricher.process([])                       # exactly one interval later

    assert len(calls) == 2


def test_long_lived_instance_rechecks_and_downloads_once_stale(monkeypatch, tmp_path):
    # Reproduces the original bug directly: an instance that is never
    # reconstructed (no process restart) but is polled for weeks must still
    # notice the CSV has crossed refresh_days and redownload it — not wait
    # forever because the age was only ever checked once, at construction.
    from modules import tar1090_db
    from modules.tar1090_db import _CHECK_INTERVAL_SECONDS, _REFRESH_DAYS

    csv_path = tmp_path / "aircraft.csv"
    db_path  = tmp_path / "aircraft.db"

    t = [1_700_000_000.0]

    def seed(path):
        path.write_text("4CA068;EI-CJX;B752;00;BOEING 757-200;;;\n", encoding="utf-8")
        import os
        os.utime(path, (t[0], t[0]))

    seed(csv_path)

    download_calls = []

    def fake_download(path):
        download_calls.append(t[0])
        seed(path)

    def fake_build(csv_p, db_p):
        db_p.write_bytes(b"")
        import os
        os.utime(db_p, (t[0], t[0]))

    monkeypatch.setattr(tar1090_db, "time", _FakeTime(t))
    monkeypatch.setattr(tar1090_db, "_download", fake_download)
    monkeypatch.setattr(tar1090_db, "_build_sqlite_db", fake_build)

    enricher = Tar1090DbEnricher(db=None, csv_path=csv_path, db_path=db_path)
    enricher.process([])   # first call: builds the index, nothing stale yet
    assert download_calls == []

    # A long-lived process, ticking hourly, well under refresh_days — no
    # download should fire yet.
    for _ in range(24 * (_REFRESH_DAYS - 1)):
        t[0] += _CHECK_INTERVAL_SECONDS
        enricher.process([])
    assert download_calls == []

    # Cross the refresh_days threshold one hourly tick at a time, exactly as
    # a receiver running for weeks without a restart would experience it.
    for _ in range(48):
        t[0] += _CHECK_INTERVAL_SECONDS
        enricher.process([])
    assert len(download_calls) == 1


# ---------------------------------------------------------------------------
# 11. refresh_days — optional config, same shape as altitude_band's edges but
#     with a sane default since nothing safety-critical rides on it.
# ---------------------------------------------------------------------------

def test_validate_refresh_days_defaults_when_absent():
    from modules.tar1090_db import _REFRESH_DAYS, _validate_refresh_days
    assert _validate_refresh_days(None) == _REFRESH_DAYS == 30


def test_validate_refresh_days_accepts_a_positive_int():
    from modules.tar1090_db import _validate_refresh_days
    assert _validate_refresh_days(7) == 7


@pytest.mark.parametrize("value", [0, -1, -30])
def test_validate_refresh_days_rejects_non_positive(value):
    from modules.tar1090_db import _validate_refresh_days
    with pytest.raises(ValueError):
        _validate_refresh_days(value)


@pytest.mark.parametrize("value", ["30", 30.0, True, [30]])
def test_validate_refresh_days_rejects_non_int(value):
    # bool is an int subclass; True as a day count is a config mistake, same
    # reasoning as altitude_band's edge validation.
    from modules.tar1090_db import _validate_refresh_days
    with pytest.raises(ValueError):
        _validate_refresh_days(value)


def test_get_reads_refresh_days_from_cfg(tmp_path, monkeypatch):
    from config import ObserverConfig
    from modules import ModuleContext, tar1090_db

    monkeypatch.setattr(tar1090_db, "_download", lambda path: (_ for _ in ()).throw(RuntimeError("no network")))
    ctx = ModuleContext(
        data_dir=tmp_path,
        module_dir=tmp_path / "modules" / "tar1090_db",
        observer=ObserverConfig(latitude=53.7778, longitude=-1.5721),
    )
    enricher = tar1090_db.get({"refresh_days": 7}, ctx)
    assert enricher._refresh_days == 7


def test_get_defaults_refresh_days_when_cfg_omits_it(tmp_path, monkeypatch):
    from config import ObserverConfig
    from modules import ModuleContext, tar1090_db

    monkeypatch.setattr(tar1090_db, "_download", lambda path: (_ for _ in ()).throw(RuntimeError("no network")))
    ctx = ModuleContext(
        data_dir=tmp_path,
        module_dir=tmp_path / "modules" / "tar1090_db",
        observer=ObserverConfig(latitude=53.7778, longitude=-1.5721),
    )
    enricher = tar1090_db.get({}, ctx)
    assert enricher._refresh_days == 30


def test_needs_refresh_respects_a_custom_refresh_days(tmp_path):
    import os
    from modules.tar1090_db import _needs_refresh

    csv_path = tmp_path / "aircraft.csv"
    csv_path.write_text("x", encoding="utf-8")
    ten_days_old = time.time() - 10 * 86400
    os.utime(csv_path, (ten_days_old, ten_days_old))

    assert _needs_refresh(csv_path, refresh_days=7) is True
    assert _needs_refresh(csv_path, refresh_days=30) is False
