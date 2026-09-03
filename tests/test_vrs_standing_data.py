"""
tests/test_vrs_standing_data.py

Tests for the vrs_standing_data data source — the full eight-table VRS
standing-data ingest into one SQLite file.

Covers:
  1. Zip download extracts all eight schema-01 trees, not just two.
  2. A partial/failed download doesn't leave a half-extracted tree or a
     half-built DB live.
  3. ensure_fresh() refreshes when the DB is older than refresh_days, not
     more often.
  4. _SCHEMA_VERSION mismatch forces a rebuild.
  5. A chunked/split file set (VLG-1/VLG-2, no VLG-all) loads every chunk
     into one logical table.
  6. A near-empty shard file (the RBB, two-line case) loads without error.
  7. All eight tables are queryable after a build.
  8. Two chains referencing source = "vrs" share one instance and therefore
     one download/build.
"""

from __future__ import annotations

import sqlite3
import time
import zipfile
from pathlib import Path

import pytest

import data_sources.vrs_standing_data as vrs_standing_data
from config import DataSourceConfig, ObserverConfig
from data_sources import DataSourceContext, clear_data_source_pool, get_data_source
from data_sources.vrs_standing_data import (
    SQLiteVrsDb, VrsStandingData, _REPO_ROOT, _SCHEMA_VERSION, _TABLES,
    _build_sqlite_db, _db_schema_version, _download_and_build, _extract_zip,
    _needs_refresh, _validate_refresh_days,
)


# ---------------------------------------------------------------------------
# Fixtures
#
# Isolate the factory pool the same way test_data_sources.py does — pooled
# instances otherwise leak across tests.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_pool():
    clear_data_source_pool()
    yield
    clear_data_source_pool()


# ---------------------------------------------------------------------------
# Fake repo tree — one small CSV per dataset folder so a build exercises the
# whole schema, plus the two shapes the brief calls out as genuinely new
# risk: a chunked airline (VLG-1/VLG-2, no VLG-all) and a near-empty shard
# (RBB, header + one data line).
# ---------------------------------------------------------------------------

def _build_repo_tree(root: Path, extra_files: dict[str, str] | None = None) -> None:
    files = {
        "aircraft/schema-01/0/00/008.csv": (
            "Hex,Registration,ModelIcao,Manufacturer,Model,ManufacturerAndModel,"
            "IsPrivateOperator,Operator,AirlineCode,SerialNumber,YearBuilt\n"
            "400008,G-TEST,B738,Boeing,737-800,Boeing 737-800,0,Test Airline,TST,12345,2010\n"
        ),
        "airlines/schema-01/E/EXS.csv": (
            "Code,Name,ICAO,IATA,PositioningFlightPattern,CharterFlightPattern\n"
            "EXS,Jet2,EXS,LS,,\n"
        ),
        "airports/schema-01/HE.csv": (
            "Code,Name,ICAO,IATA,Location,CountryISO2,Latitude,Longitude,AltitudeFeet\n"
            "HE34,Test Airport,HEAA,,Cairo,EG,30.1,31.4,382\n"
        ),
        "code-blocks/schema-01/00.csv": (
            "Start,Finish,Count,Bitmask,SignificantBitmask,IsMilitary,CountryISO2\n"
            "000000,0003FF,1024,000000000000000000000000,111111111111111111111100,0,US\n"
        ),
        "countries/schema-01/countries.csv": (
            "ISO,Name\nGB,United Kingdom\n"
        ),
        "model-type/schema-01/B738.csv": (
            "ICAO,Manufacturer,Model,Engines,EngineTypeCode,EnginePlacementCode,"
            "SpeciesCode,WakeTurbulenceCode,IsActive\n"
            "B738,BOEING,737-800,2,J,W,L,M,1\n"
        ),
        "registration-prefixes/schema-01/G.csv": (
            "Prefix,CountryISO2,HasHyphen,DecodeFullRegex,DecodeNoHyphenRegex,FormatTemplate\n"
            "G,GB,1,^G-[A-Z]{4}$,^G[A-Z]{4}$,G-####\n"
        ),
        # Chunked large-airline case: VLG-1.csv / VLG-2.csv, deliberately no
        # VLG-all.csv — confirming the loader's own glob is what stitches
        # these together, not a filename convention.
        "routes/schema-01/V/VLG-1.csv": (
            "Callsign,Code,Number,AirlineCode,AirportCodes\n"
            "VLG1001,VY,1001,VLG,LEBL-EGLL\n"
        ),
        "routes/schema-01/V/VLG-2.csv": (
            "Callsign,Code,Number,AirlineCode,AirportCodes\n"
            "VLG2002,VY,2002,VLG,LEMD-EGKK\n"
        ),
        # Near-empty shard: header plus a single data line, the RBB case.
        "routes/schema-01/R/RBB-all.csv": (
            "Callsign,Code,Number,AirlineCode,AirportCodes\n"
            "RBB123,BB,123,RBB,EGKK-EGCC\n"
        ),
    }
    if extra_files:
        files.update(extra_files)
    for rel_path, content in files.items():
        path = root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _zip_repo(repo_dir: Path, zip_path: Path) -> None:
    """Zip so the archive root is `standing-data-main/...`, matching
    codeload's real archive layout."""
    with zipfile.ZipFile(zip_path, "w") as zf:
        for path in repo_dir.rglob("*"):
            if path.is_file():
                zf.write(path, path.relative_to(repo_dir.parent))


@pytest.fixture
def fake_zip(tmp_path) -> Path:
    repo_dir = tmp_path / "src" / _REPO_ROOT
    _build_repo_tree(repo_dir)
    zip_path = tmp_path / "src.zip"
    _zip_repo(repo_dir, zip_path)
    return zip_path


def _ctx(tmp_path: Path, name: str = "vrs") -> DataSourceContext:
    return DataSourceContext(data_dir=tmp_path, source_dir=tmp_path / "data_sources" / name)


# ===========================================================================
# 1. Extraction — all eight trees, not just two
# ===========================================================================

def test_extraction_produces_all_eight_dataset_trees(tmp_path, fake_zip):
    extract_root = tmp_path / "extract"
    repo_root = _extract_zip(fake_zip, extract_root)

    expected_folders = {t.folder for t in _TABLES}
    assert expected_folders == {
        "aircraft", "airlines", "airports", "code-blocks",
        "countries", "model-type", "registration-prefixes", "routes",
    }
    for folder in expected_folders:
        assert (repo_root / folder / "schema-01").is_dir()


# ===========================================================================
# 2. Partial/failed download leaves nothing half-built live
# ===========================================================================

def test_corrupt_zip_leaves_no_half_extracted_tree(tmp_path, monkeypatch):
    directory = tmp_path / "vrs"
    db_path = directory / "standing_data.db"

    def fake_download_zip(zip_path: Path) -> None:
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        zip_path.write_bytes(b"not actually a zip file")

    monkeypatch.setattr(vrs_standing_data, "_download_zip", fake_download_zip)

    with pytest.raises(zipfile.BadZipFile):
        _download_and_build(directory, db_path)

    assert not (directory / "standing-data-extracted").exists()
    assert not (directory / "standing-data-extracted.scratch").exists()
    assert not db_path.exists()


def test_ensure_fresh_keeps_cached_db_when_download_fails(tmp_path, fake_zip, monkeypatch):
    ctx = _ctx(tmp_path)
    source = VrsStandingData({}, ctx)

    monkeypatch.setattr(
        vrs_standing_data, "_download_zip",
        lambda zip_path: zip_path.write_bytes(b"garbage"),
    )
    # No cached DB exists yet — a failed first build must leave the source
    # with no usable db rather than raising out of ensure_fresh().
    source.ensure_fresh()
    assert source.db is None
    assert not source._db_path.exists()

    # Now succeed once, so a cached DB exists ...
    def real_download(zip_path: Path) -> None:
        import shutil
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(fake_zip, zip_path)

    monkeypatch.setattr(vrs_standing_data, "_download_zip", real_download)
    source.ensure_fresh()
    assert source.db is not None
    good_db = source.db

    # ... then fail again on a forced refresh: the cached DB must survive.
    import os
    old = time.time() - 30 * 86400
    os.utime(source._db_path, (old, old))
    monkeypatch.setattr(
        vrs_standing_data, "_download_zip",
        lambda zip_path: (_ for _ in ()).throw(RuntimeError("network down")),
    )
    source.ensure_fresh()
    assert source.db is good_db
    assert source._db_path.exists()


# ===========================================================================
# 3. ensure_fresh() — refreshes when older than refresh_days, not more often
# ===========================================================================

def test_ensure_fresh_does_not_rebuild_before_refresh_days_elapse(tmp_path, fake_zip, monkeypatch):
    calls = []

    def fake_build(directory, db_path):
        calls.append(1)
        import shutil
        directory.mkdir(parents=True, exist_ok=True)
        extract_root = directory / "extracted"
        with zipfile.ZipFile(fake_zip) as zf:
            zf.extractall(directory / "extracted")
        _build_sqlite_db(extract_root / _REPO_ROOT, db_path)
        shutil.rmtree(extract_root, ignore_errors=True)

    monkeypatch.setattr(vrs_standing_data, "_download_and_build", fake_build)

    ctx = _ctx(tmp_path)
    source = VrsStandingData({"refresh_days": 7}, ctx)
    for _ in range(5):
        source.ensure_fresh()
    assert calls == [1]


def test_ensure_fresh_rebuilds_once_db_is_older_than_refresh_days(tmp_path, fake_zip, monkeypatch):
    import os

    calls = []

    def fake_build(directory, db_path):
        calls.append(1)
        directory.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(fake_zip) as zf:
            zf.extractall(directory / "extracted")
        _build_sqlite_db(directory / "extracted" / _REPO_ROOT, db_path)

    monkeypatch.setattr(vrs_standing_data, "_download_and_build", fake_build)

    ctx = _ctx(tmp_path)
    source = VrsStandingData({"refresh_days": 7}, ctx)
    source.ensure_fresh()
    assert calls == [1]

    stale = time.time() - 8 * 86400
    os.utime(source._db_path, (stale, stale))
    source.ensure_fresh()
    assert calls == [1, 1]


def test_needs_refresh_respects_custom_refresh_days(tmp_path):
    import os
    db_path = tmp_path / "standing_data.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
    conn.commit()
    conn.close()
    ten_days_old = time.time() - 10 * 86400
    os.utime(db_path, (ten_days_old, ten_days_old))

    assert _needs_refresh(db_path, refresh_days=7) is True
    assert _needs_refresh(db_path, refresh_days=30) is False


def test_needs_refresh_true_when_db_missing(tmp_path):
    assert _needs_refresh(tmp_path / "absent.db", refresh_days=7) is True


# ===========================================================================
# 4. Schema version — a mismatch forces a rebuild regardless of age
# ===========================================================================

def test_schema_version_mismatch_forces_rebuild_even_when_fresh_by_age(tmp_path, fake_zip):
    extract_root = tmp_path / "extract"
    repo_root = _extract_zip(fake_zip, extract_root)
    db_path = tmp_path / "standing_data.db"
    _build_sqlite_db(repo_root, db_path)

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.close()

    assert _db_schema_version(db_path) != _SCHEMA_VERSION
    assert _needs_refresh(db_path, refresh_days=7) is True   # not aged, but version mismatch


def test_missing_db_reports_version_zero(tmp_path):
    assert _db_schema_version(tmp_path / "absent.db") == 0


def test_built_db_carries_the_current_schema_version(tmp_path, fake_zip):
    extract_root = tmp_path / "extract"
    repo_root = _extract_zip(fake_zip, extract_root)
    db_path = tmp_path / "standing_data.db"
    _build_sqlite_db(repo_root, db_path)
    assert _db_schema_version(db_path) == _SCHEMA_VERSION


# ===========================================================================
# 5. Chunked file set — VLG-1/VLG-2, no VLG-all, both load into one table
# ===========================================================================

def test_chunked_route_files_all_load_into_one_table(tmp_path, fake_zip):
    extract_root = tmp_path / "extract"
    repo_root = _extract_zip(fake_zip, extract_root)
    db_path = tmp_path / "standing_data.db"
    _build_sqlite_db(repo_root, db_path)

    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT callsign FROM routes WHERE callsign IN ('VLG1001', 'VLG2002')"
    ).fetchall()
    conn.close()
    assert sorted(r[0] for r in rows) == ["VLG1001", "VLG2002"]


# ===========================================================================
# 6. Near-empty shard file — header plus one line, loads without error
# ===========================================================================

def test_near_empty_shard_file_loads_without_error(tmp_path, fake_zip):
    extract_root = tmp_path / "extract"
    repo_root = _extract_zip(fake_zip, extract_root)
    db_path = tmp_path / "standing_data.db"
    _build_sqlite_db(repo_root, db_path)   # must not raise

    db = SQLiteVrsDb(db_path)
    assert db.get_route("RBB123") == ("BB", 123, "RBB", "EGKK-EGCC")


# ===========================================================================
# 7. All eight tables are queryable after a build
# ===========================================================================

def test_all_eight_tables_are_queryable_and_nonempty(tmp_path, fake_zip):
    extract_root = tmp_path / "extract"
    repo_root = _extract_zip(fake_zip, extract_root)
    db_path = tmp_path / "standing_data.db"
    _build_sqlite_db(repo_root, db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        for table in _TABLES:
            count = conn.execute(f"SELECT COUNT(*) FROM {table.name}").fetchone()[0]
            assert count > 0, f"{table.name} table is empty"
    finally:
        conn.close()


def test_get_route_returns_expected_columns(tmp_path, fake_zip):
    extract_root = tmp_path / "extract"
    repo_root = _extract_zip(fake_zip, extract_root)
    db_path = tmp_path / "standing_data.db"
    _build_sqlite_db(repo_root, db_path)

    db = SQLiteVrsDb(db_path)
    assert db.get_route("VLG1001") == ("VY", 1001, "VLG", "LEBL-EGLL")
    assert db.get_route("NOSUCH") is None


# ===========================================================================
# New accessors for vrs_route (brief-vrs-route.md rev 2): get_airport(),
# get_country(), get_airline()
# ===========================================================================

def test_get_airport_is_keyed_on_icao_not_code(tmp_path, fake_zip):
    # airports/schema-01/HE.csv (real sample) has a row where `Code` (HE34)
    # differs from `ICAO` (also HE34 in this fixture's row, so key on a
    # deliberately different pair to prove the join column, not the row).
    extra = {
        "airports/schema-01/ZZ/ZZ.csv": (
            "Code,Name,ICAO,IATA,Location,CountryISO2,Latitude,Longitude,AltitudeFeet\n"
            "ZZCODE,Mismatch Airport,ZZICAO,MI,Mismatch City,ZZ,1.0,2.0,100\n"
        ),
    }
    repo_dir = tmp_path / "src" / vrs_standing_data._REPO_ROOT
    _build_repo_tree(repo_dir, extra)
    zip_path = tmp_path / "src2.zip"
    _zip_repo(repo_dir, zip_path)

    extract_root = tmp_path / "extract"
    repo_root = _extract_zip(zip_path, extract_root)
    db_path = tmp_path / "standing_data.db"
    _build_sqlite_db(repo_root, db_path)

    db = SQLiteVrsDb(db_path)
    assert db.get_airport("ZZICAO") == ("Mismatch Airport", "MI", "Mismatch City", "ZZ")
    assert db.get_airport("ZZCODE") is None   # the Code value must not match


def test_get_airport_returns_none_on_miss(tmp_path, fake_zip):
    extract_root = tmp_path / "extract"
    repo_root = _extract_zip(fake_zip, extract_root)
    db_path = tmp_path / "standing_data.db"
    _build_sqlite_db(repo_root, db_path)

    db = SQLiteVrsDb(db_path)
    assert db.get_airport("NOSUCH") is None


def test_get_country_returns_row_on_hit_and_none_on_miss(tmp_path, fake_zip):
    extract_root = tmp_path / "extract"
    repo_root = _extract_zip(fake_zip, extract_root)
    db_path = tmp_path / "standing_data.db"
    _build_sqlite_db(repo_root, db_path)

    db = SQLiteVrsDb(db_path)
    assert db.get_country("GB").name == "United Kingdom"
    assert db.get_country("ZZ") is None


def test_get_airline_returns_row_on_hit_and_none_on_miss(tmp_path, fake_zip):
    extract_root = tmp_path / "extract"
    repo_root = _extract_zip(fake_zip, extract_root)
    db_path = tmp_path / "standing_data.db"
    _build_sqlite_db(repo_root, db_path)

    db = SQLiteVrsDb(db_path)
    assert db.get_airline("EXS").name == "Jet2"
    assert db.get_airline("NOSUCH") is None


# ===========================================================================
# 8. Pooling — two chains naming the same source share one download/build
# ===========================================================================

@pytest.fixture
def installed_config(monkeypatch, tmp_path):
    from config import config as squawk_config

    monkeypatch.setattr(squawk_config.squawk, "data_dir", str(tmp_path))
    monkeypatch.setattr(squawk_config, "data_sources", {
        "vrs": DataSourceConfig(
            name="vrs", type="vrs_standing_data", cfg={"type": "vrs_standing_data"},
        ),
    })
    return squawk_config


def test_two_chains_naming_the_same_source_share_one_download(
    installed_config, tmp_path, fake_zip, monkeypatch,
):
    calls = []

    def fake_build(directory, db_path):
        calls.append(1)
        directory.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(fake_zip) as zf:
            zf.extractall(directory / "extracted")
        _build_sqlite_db(directory / "extracted" / _REPO_ROOT, db_path)

    monkeypatch.setattr(vrs_standing_data, "_download_and_build", fake_build)

    cfg = {"type": "vrs_standing_data"}
    first  = get_data_source("vrs", cfg)
    second = get_data_source("vrs", cfg)
    assert first is second

    first.ensure_fresh()
    second.ensure_fresh()
    assert calls == [1]


# ===========================================================================
# refresh_days validation — same shape as tar1090_db's
# ===========================================================================

def test_validate_refresh_days_defaults_when_absent():
    assert _validate_refresh_days(None) == 7


def test_validate_refresh_days_accepts_a_positive_int():
    assert _validate_refresh_days(3) == 3


@pytest.mark.parametrize("value", [0, -1, -7])
def test_validate_refresh_days_rejects_non_positive(value):
    with pytest.raises(ValueError):
        _validate_refresh_days(value)


@pytest.mark.parametrize("value", ["7", 7.0, True, [7]])
def test_validate_refresh_days_rejects_non_int(value):
    with pytest.raises(ValueError):
        _validate_refresh_days(value)


def test_directory_sits_under_data_sources_keyed_on_block_name(tmp_path):
    ctx = _ctx(tmp_path, "vrs")
    source = VrsStandingData({}, ctx)
    assert source.directory == tmp_path / "data_sources" / "vrs"
