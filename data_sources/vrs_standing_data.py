"""
data_sources/vrs_standing_data.py

Downloads the entire Virtual Radar Server standing-data repo
(vradarserver/standing-data) and loads all eight schema-01 datasets into one
SQLite file: aircraft, airlines, airports, code_blocks, countries, model_type,
registration_prefixes, routes.

Every dataset goes in on the first build rather than adding tables one
consumer at a time — today's only consumer is routes, but the whole repo is
one zip download regardless, so there is no cost to loading the rest now
against having to come back and touch this module once per future consumer.

Fetch is a whole-repo zip via GitHub's archive endpoint (not a git clone — no
history needed), same mechanism as tar1090_db._download's CSV fetch, just a
zip instead of a gzip'd CSV. Extraction and the SQLite build both go through
a scratch-path-then-atomic-replace, same temp-and-replace pattern
DiskDriveStorage uses for its writes, so a failed or partial download or
build never leaves a half-extracted tree or a half-built DB live. The
extracted CSVs are discarded once the build succeeds — they're not needed
after that, and re-extracting on the next refresh is cheap.

Each dataset folder is globbed as `<dataset>/schema-01/**/*.csv` rather than
assuming a filename pattern or an `-all` suffix: some datasets (e.g. large
airline route sets) split into numbered chunk files instead of one file, and
a dataset's row count has no relationship to its file count.

Refresh is weekly by elapsed days, checked against the built DB file's own
mtime — no separate state file, same freshness-clock reuse tar1090_db applies
to its CSV. This is a different policy shape from tar1090_db's identical-
looking one: both are "stale after N days", but tar1090_db checks the CSV's
age while this checks the built DB's age directly, since there's no
intermediate cached file worth keeping around between builds.

The SQLite index is rebuilt when the DB is missing, older than
`refresh_days`, or its _SCHEMA_VERSION doesn't match — a DB built by an older
Squawk has a different table shape and must be discarded rather than read.
"""

from __future__ import annotations

import csv
import shutil
import sqlite3
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, NamedTuple

import requests

from data_sources import BaseDataSource, DataSourceContext


_ZIP_URL      = "https://codeload.github.com/vradarserver/standing-data/zip/refs/heads/main"
_REPO_ROOT    = "standing-data-main"          # folder name inside the zip
_REFRESH_DAYS = 7
_DB_FILENAME  = "standing_data.db"

# Bump when any table's shape changes. One version covers all eight tables —
# a table shape only ever changes alongside a fresh repo pull, there's no
# scenario where just one table's shape moves independently of the others.
_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Column converters — TEXT fields collapse "" to None (UNKNOWN sentinel,
# same convention the rest of the schema uses); numeric fields do the same
# and additionally tolerate a malformed value by returning None rather than
# raising, since a bad row degrading to UNKNOWN beats aborting the whole build.
# ---------------------------------------------------------------------------

def _text(raw: str) -> str | None:
    raw = raw.strip()
    return raw or None


def _int(raw: str) -> int | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _real(raw: str) -> float | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _bool_int(raw: str) -> int | None:
    raw = raw.strip().lower()
    if raw in ("1", "true", "yes", "y"):
        return 1
    if raw in ("0", "false", "no", "n"):
        return 0
    return None


@dataclass(frozen=True)
class _Column:
    name:      str
    sql_type:  str
    convert:   Callable[[str], object]


@dataclass(frozen=True)
class _Table:
    folder:         str            # dataset folder under the extracted repo root
    name:           str            # SQL table name
    source_header:  str            # original CSV header, for schema-diff visibility
    columns:        list[_Column]
    primary_key:    str | None = None
    index_columns:  tuple[str, ...] = ()


# Column order matches each dataset's CSV column order exactly (confirmed
# against real routes/ and airports/ samples; the other six follow the same
# per-dataset schema-01 convention).
_TABLES = [
    _Table(
        folder="aircraft", name="aircraft",
        source_header="Hex,Registration,ModelIcao,Manufacturer,Model,"
                       "ManufacturerAndModel,IsPrivateOperator,Operator,"
                       "AirlineCode,SerialNumber,YearBuilt",
        columns=[
            _Column("hex",                    "TEXT",    _text),
            _Column("registration",           "TEXT",    _text),
            _Column("model_icao",             "TEXT",    _text),
            _Column("manufacturer",           "TEXT",    _text),
            _Column("model",                  "TEXT",    _text),
            _Column("manufacturer_and_model", "TEXT",    _text),
            _Column("is_private_operator",    "INTEGER", _bool_int),
            _Column("operator",               "TEXT",    _text),
            _Column("airline_code",           "TEXT",    _text),
            _Column("serial_number",          "TEXT",    _text),
            _Column("year_built",             "INTEGER", _int),
        ],
        primary_key="hex",
    ),
    _Table(
        folder="airlines", name="airlines",
        source_header="Code,Name,ICAO,IATA,PositioningFlightPattern,CharterFlightPattern",
        columns=[
            _Column("code",                       "TEXT", _text),
            _Column("name",                       "TEXT", _text),
            _Column("icao",                       "TEXT", _text),
            _Column("iata",                       "TEXT", _text),
            _Column("positioning_flight_pattern",  "TEXT", _text),
            _Column("charter_flight_pattern",      "TEXT", _text),
        ],
        index_columns=("code", "icao"),
    ),
    _Table(
        folder="airports", name="airports",
        source_header="Code,Name,ICAO,IATA,Location,CountryISO2,Latitude,Longitude,AltitudeFeet",
        columns=[
            _Column("code",          "TEXT",    _text),
            _Column("name",          "TEXT",    _text),
            _Column("icao",          "TEXT",    _text),
            _Column("iata",          "TEXT",    _text),
            _Column("location",      "TEXT",    _text),
            _Column("country_iso2",  "TEXT",    _text),
            _Column("latitude",      "REAL",    _real),
            _Column("longitude",     "REAL",    _real),
            _Column("altitude_feet", "INTEGER", _int),
        ],
        index_columns=("code", "icao"),
    ),
    _Table(
        folder="code-blocks", name="code_blocks",
        source_header="Start,Finish,Count,Bitmask,SignificantBitmask,IsMilitary,CountryISO2",
        columns=[
            _Column("start",               "TEXT",    _text),
            _Column("finish",              "TEXT",    _text),
            _Column("count",               "INTEGER", _int),
            _Column("bitmask",             "TEXT",    _text),
            _Column("significant_bitmask", "TEXT",    _text),
            _Column("is_military",         "INTEGER", _bool_int),
            _Column("country_iso2",        "TEXT",    _text),
        ],
        index_columns=("start", "finish"),
    ),
    _Table(
        folder="countries", name="countries",
        source_header="ISO,Name",
        columns=[
            _Column("iso",  "TEXT", _text),
            _Column("name", "TEXT", _text),
        ],
        primary_key="iso",
    ),
    _Table(
        folder="model-type", name="model_type",
        source_header="ICAO,Manufacturer,Model,Engines,EngineTypeCode,"
                       "EnginePlacementCode,SpeciesCode,WakeTurbulenceCode,IsActive",
        columns=[
            _Column("icao",                  "TEXT",    _text),
            _Column("manufacturer",          "TEXT",    _text),
            _Column("model",                 "TEXT",    _text),
            _Column("engines",               "INTEGER", _int),
            _Column("engine_type_code",      "TEXT",    _text),
            _Column("engine_placement_code", "TEXT",    _text),
            _Column("species_code",          "TEXT",    _text),
            _Column("wake_turbulence_code",  "TEXT",    _text),
            _Column("is_active",             "INTEGER", _bool_int),
        ],
        index_columns=("icao",),
    ),
    _Table(
        folder="registration-prefixes", name="registration_prefixes",
        source_header="Prefix,CountryISO2,HasHyphen,DecodeFullRegex,"
                       "DecodeNoHyphenRegex,FormatTemplate",
        columns=[
            _Column("prefix",                  "TEXT",    _text),
            _Column("country_iso2",            "TEXT",    _text),
            _Column("has_hyphen",              "INTEGER", _bool_int),
            _Column("decode_full_regex",       "TEXT",    _text),
            _Column("decode_no_hyphen_regex",  "TEXT",    _text),
            _Column("format_template",         "TEXT",    _text),
        ],
        index_columns=("prefix",),
    ),
    _Table(
        folder="routes", name="routes",
        source_header="Callsign,Code,Number,AirlineCode,AirportCodes",
        columns=[
            _Column("callsign",      "TEXT",    _text),
            _Column("code",          "TEXT",    _text),
            _Column("number",        "INTEGER", _int),
            _Column("airline_code",  "TEXT",    _text),
            _Column("airport_codes", "TEXT",    _text),
        ],
        index_columns=("callsign",),
    ),
]


def _validate_refresh_days(value: object) -> int:
    """Return `value` as a positive int, or `_REFRESH_DAYS` if unset.

    Same shape as tar1090_db's `_validate_refresh_days` — a sane default
    exists and nothing safety-critical rides on it, so only a present-but-
    nonsensical value is an error.
    """
    if value is None:
        return _REFRESH_DAYS
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"vrs_standing_data: 'refresh_days' must be an integer, got {value!r}")
    if value <= 0:
        raise ValueError(f"vrs_standing_data: 'refresh_days' must be positive, got {value!r}")
    return value


# ---------------------------------------------------------------------------
# Query surface
# ---------------------------------------------------------------------------

class AirportRow(NamedTuple):
    name:         str | None
    iata:         str | None
    location:     str | None   # municipality the airport serves, e.g. "Reus"
    country_iso2: str | None


class CountryRow(NamedTuple):
    name: str | None


class AirlineRow(NamedTuple):
    name: str | None


class SQLiteVrsDb:
    """Thread-local read connection to the built standing-data SQLite file.

    Module instances that use this may be shared across chains running in
    separate threads, so a single shared sqlite3.Connection can't be assumed
    safe — same reasoning as SQLiteTarDb.

    Query methods are added one at a time, as a consumer actually needs them
    — see the module docstring for why the other five tables have none yet.
    get_route() is the original; get_airport()/get_country()/get_airline()
    were added for vrs_route (brief-vrs-route.md rev 2).
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._local = threading.local()

    def _cursor(self) -> sqlite3.Cursor:
        if not hasattr(self._local, "cursor"):
            conn = sqlite3.connect(str(self._db_path), timeout=5)
            self._local.conn = conn
            self._local.cursor = conn.cursor()
        return self._local.cursor

    def get_route(self, callsign: str) -> tuple[str | None, int | None, str | None, str | None] | None:
        """(code, number, airline_code, airport_codes) for `callsign`, or None."""
        cursor = self._cursor()
        cursor.execute(
            "SELECT code, number, airline_code, airport_codes FROM routes WHERE callsign = ?",
            (callsign,),
        )
        return cursor.fetchone()

    def get_airport(self, icao: str) -> AirportRow | None:
        """Airport keyed on `airports.icao` — confirmed the correct join
        column for `routes.airport_codes` values, which are ICAO codes.
        `airports.code` happens to equal `icao` in at least one real shard
        but is not guaranteed to in general; don't join on it instead."""
        cursor = self._cursor()
        cursor.execute(
            "SELECT name, iata, location, country_iso2 FROM airports WHERE icao = ?",
            (icao,),
        )
        row = cursor.fetchone()
        return AirportRow(*row) if row else None

    def get_country(self, iso2: str) -> CountryRow | None:
        """Country keyed on `countries.iso`."""
        cursor = self._cursor()
        cursor.execute("SELECT name FROM countries WHERE iso = ?", (iso2,))
        row = cursor.fetchone()
        return CountryRow(*row) if row else None

    def get_airline(self, code: str) -> AirlineRow | None:
        """Airline keyed on `airlines.code`, joined against
        `routes.airline_code` — not `airlines.icao`."""
        cursor = self._cursor()
        cursor.execute("SELECT name FROM airlines WHERE code = ?", (code,))
        row = cursor.fetchone()
        return AirlineRow(*row) if row else None


# ---------------------------------------------------------------------------
# Fetch + build
# ---------------------------------------------------------------------------

def _download_zip(zip_path: Path) -> None:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"  vrs_standing_data: downloading standing-data from {_ZIP_URL} …")
    with requests.get(_ZIP_URL, stream=True, timeout=60) as response:
        response.raise_for_status()
        tmp = zip_path.with_suffix(".part")
        with open(tmp, "wb") as f:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
        tmp.replace(zip_path)


def _extract_zip(zip_path: Path, extract_root: Path) -> Path:
    """Extract `zip_path` to a scratch dir, then atomically replace
    `extract_root` with it. Returns the repo root inside `extract_root`.

    A zip that fails to open or extract raises before `extract_root` is
    touched, so a partial/corrupt download never leaves a half-extracted
    tree live at the path the build reads from.
    """
    scratch = extract_root.with_name(extract_root.name + ".scratch")
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(scratch)
    except Exception:
        shutil.rmtree(scratch, ignore_errors=True)
        raise

    if extract_root.exists():
        shutil.rmtree(extract_root)
    scratch.replace(extract_root)
    return extract_root / _REPO_ROOT


def _create_table_sql(table: _Table) -> str:
    col_defs = []
    for c in table.columns:
        col_def = f"{c.name} {c.sql_type}"
        if c.name == table.primary_key:
            col_def += " PRIMARY KEY"
        col_defs.append(col_def)
    return f"CREATE TABLE {table.name} ({', '.join(col_defs)})"


def _load_table(cur: sqlite3.Cursor, repo_root: Path, table: _Table) -> None:
    # table.source_header carries the original CSV header for this table
    # (see _TABLES above), preserved right beside the column list so a
    # future schema diff against a real sample file is easy to spot.
    cur.execute(_create_table_sql(table))

    insert_sql = f"INSERT INTO {table.name} VALUES ({', '.join('?' for _ in table.columns)})"
    batch: list[tuple] = []
    csv_files = sorted((repo_root / table.folder / "schema-01").glob("**/*.csv"))
    for csv_path in csv_files:
        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if header is None:
                continue   # empty file, not even a header
            for row in reader:
                if not row:
                    continue
                values = tuple(
                    col.convert(row[i] if i < len(row) else "")
                    for i, col in enumerate(table.columns)
                )
                batch.append(values)
                if len(batch) >= 50000:
                    cur.executemany(insert_sql, batch)
                    batch = []
    if batch:
        cur.executemany(insert_sql, batch)

    for idx_col in table.index_columns:
        cur.execute(f"CREATE INDEX idx_{table.name}_{idx_col} ON {table.name} ({idx_col})")


def _build_sqlite_db(repo_root: Path, db_path: Path) -> None:
    tmp_db = db_path.with_suffix(".tmp")
    tmp_db.unlink(missing_ok=True)
    conn = sqlite3.connect(str(tmp_db))
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA synchronous = OFF")
        cur.execute("PRAGMA journal_mode = MEMORY")
        for table in _TABLES:
            _load_table(cur, repo_root, table)
        cur.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        conn.commit()
    finally:
        conn.close()
    tmp_db.replace(db_path)


def _download_and_build(directory: Path, db_path: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    zip_path     = directory / "standing-data.zip.tmp"
    extract_root = directory / "standing-data-extracted"
    try:
        _download_zip(zip_path)
        repo_root = _extract_zip(zip_path, extract_root)
        print(f"  vrs_standing_data: building SQLite index {db_path.name} …")
        _build_sqlite_db(repo_root, db_path)
        print(f"  vrs_standing_data: saved to {db_path}")
    finally:
        zip_path.unlink(missing_ok=True)
        if extract_root.exists():
            shutil.rmtree(extract_root, ignore_errors=True)


def _db_schema_version(db_path: Path) -> int:
    """The DB's schema version, or 0 if it is missing or unreadable."""
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        try:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])
        finally:
            conn.close()
    except sqlite3.Error:
        return 0


def _needs_refresh(db_path: Path, refresh_days: int) -> bool:
    if not db_path.exists():
        return True
    age_seconds = time.time() - db_path.stat().st_mtime
    if age_seconds > refresh_days * 86400:
        return True
    return _db_schema_version(db_path) != _SCHEMA_VERSION


# ---------------------------------------------------------------------------
# BaseDataSource
# ---------------------------------------------------------------------------

class VrsStandingData(BaseDataSource):

    def __init__(self, cfg: dict, ctx: DataSourceContext) -> None:
        self._dir = ctx.source_dir
        self._db_path = self._dir / _DB_FILENAME
        self._refresh_days = _validate_refresh_days(cfg.get("refresh_days"))
        self._lock = threading.Lock()
        # None until a build is confirmed present on disk — set from
        # ensure_fresh(), never assumed at construction. Mirrors
        # Tar1090DbEnricher: a db file already sitting on disk from a
        # previous run is not trusted until the freshness check has actually
        # run against it.
        self._db: SQLiteVrsDb | None = None

    def ensure_fresh(self) -> None:
        with self._lock:
            rebuilt = False
            if _needs_refresh(self._db_path, self._refresh_days):
                try:
                    _download_and_build(self._dir, self._db_path)
                    rebuilt = True
                except Exception as exc:
                    if not self._db_path.exists():
                        print(f"  vrs_standing_data: download failed ({exc}), no data available")
                        return
                    print(f"  vrs_standing_data: refresh failed ({exc}), using cached data")

            if rebuilt or self._db is None:
                # A fresh SQLiteVrsDb, not a reused one: its thread-local
                # connections are opened lazily, so this guarantees every
                # thread reads the file that's actually on disk now rather
                # than one it had already opened before a rebuild.
                self._db = SQLiteVrsDb(self._db_path)

    @property
    def directory(self) -> Path:
        return self._dir

    @property
    def db(self) -> SQLiteVrsDb | None:
        """The query wrapper, or None if no build has ever succeeded.

        A consumer must guard this the same way Tar1090DbEnricher guards its
        own db — `if source.db is None: return aircraft` — rather than
        assuming ensure_fresh() has already produced a usable file.
        """
        return self._db


KEYS = {"refresh_days"}


def get(cfg: dict, ctx: DataSourceContext) -> VrsStandingData:
    return VrsStandingData(cfg, ctx)
