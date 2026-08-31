"""
modules/tar1090_db.py

Enriches Aircraft records with registration, ICAO type code, type
description and the dbFlags bitfield from the tar1090 aircraft database
(aircraft.csv), a semicolon-delimited CSV.

Only fills fields that are currently UNKNOWN (None) — never overwrites
data already supplied by the source.

CSV format (no header, semicolon-delimited, 8 fields):
    hex ; registration ; type_code ; flags ; description ; ... (remaining unused)

The type code and the description are both carried through as separate
fields. They were once collapsed into one value, preferring the
description, which threw away the only machine-readable identifier of
the two.

The flags column is a little-endian bit string, not a number — see
_parse_db_flags. It feeds airframe.db_flags, which adsbdb consults to
avoid looking up aircraft whose operator has requested suppression.

The CSV is downloaded automatically from the tar1090-db GitHub release if it
is missing or older than 30 days, then cached at:
    <data_dir>/modules/tar1090_db/aircraft.csv

The SQLite index is rebuilt when the CSV is newer than it, or when its
_SCHEMA_VERSION does not match — an index built by an older Squawk has a
different column count and must be discarded rather than read.
"""

import csv
import gzip
import sqlite3
import sys
import threading
import time
from pathlib import Path

import requests

from modules import BaseModule, ModuleContext
from schemas.aircraft import Aircraft


_CSV_URL      = "https://github.com/wiedehopf/tar1090-db/raw/refs/heads/csv/aircraft.csv.gz"
_REFRESH_DAYS = 30

# Bump when the aircraft table's shape changes. Stored in the database's
# PRAGMA user_version; a mismatch forces a rebuild. Version 1 was the
# two-column (reg, type_code) index where type_code actually held whichever
# of the description or the code was present; version 2 added the separate
# description column; version 3 added flags.
_SCHEMA_VERSION = 3


class SQLiteTarDb:

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._local = threading.local()

    def get(self, hex_code: str) -> tuple[str | None, str | None, str | None, int | None] | None:
        if not hasattr(self._local, "conn"):
            self._local.conn = sqlite3.connect(str(self._db_path), timeout=5)
            self._local.cursor = self._local.conn.cursor()
        self._local.cursor.execute(
            "SELECT reg, type_code, description, flags FROM aircraft WHERE hex = ?", (hex_code,)
        )
        return self._local.cursor.fetchone()


class Tar1090DbEnricher(BaseModule):

    def __init__(self, db: dict[str, tuple[str | None, str | None, str | None, int | None]] | SQLiteTarDb | None = None) -> None:
        # icao_hex (uppercase) → (registration, type_code, description, db_flags)
        self._db = db

    def process(self, aircraft: list[Aircraft]) -> list[Aircraft]:
        if not self._db:
            return aircraft
        for a in aircraft:
            if not a.meta.icao_hex:
                continue
            row = self._db.get(a.meta.icao_hex)
            if row is None:
                continue
            reg, type_code, description, db_flags = row
            if a.airframe.registration is None and reg:
                a.airframe.registration = reg
            # Filled independently: a row may carry a code with no description.
            if a.airframe.type_code is None and type_code:
                a.airframe.type_code = type_code
            if a.airframe.type_description is None and description:
                a.airframe.type_description = description
            # `is not None`, not truthiness: 0 means "no flags set", which is a
            # fact worth storing and is different from "we don't know".
            if a.airframe.db_flags is None and db_flags is not None:
                a.airframe.db_flags = db_flags
        return aircraft


def _parse_db_flags(raw: str) -> int | None:
    """Parse the CSV's flags column into the tar1090 dbFlags bitfield.

    The column is a **little-endian bit string**, not a decimal or a hex
    number: the character at index i is bit i. So '0010' is bit 2 (PIA),
    '0001' is bit 3 (LADD), and '11' is bits 0|1 (military + interesting).
    Trailing characters are padding and carry no meaning.

    Reading it as decimal or hex silently produces plausible-looking wrong
    flags for every aircraft, which is worse than having none. Verified
    against the shipped database: bit 2 is set on 50,423 rows that are 100%
    US-allocated (PIA addresses come from the US ICAO block by construction),
    and bit 0 is 16x enriched among military airframe types.

    Returns None when the column is empty or malformed — "we don't know",
    which is not the same as 0, "no flags set".
    """
    s = raw.strip()
    if not s or any(c not in "01" for c in s):
        return None
    return sum(1 << i for i, c in enumerate(s) if c == "1")


def _needs_refresh(csv_path: Path) -> bool:
    if not csv_path.exists():
        return True
    age_seconds = time.time() - csv_path.stat().st_mtime
    return age_seconds > _REFRESH_DAYS * 86400


def _download(csv_path: Path) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"  tar1090_db: downloading aircraft database from {_CSV_URL} …")
    response = requests.get(_CSV_URL, timeout=30)
    response.raise_for_status()
    data = gzip.decompress(response.content)
    tmp  = csv_path.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(csv_path)
    print(f"  tar1090_db: saved to {csv_path}")


def _build_sqlite_db(csv_path: Path, db_path: Path) -> None:
    tmp_db = db_path.with_suffix(".tmp")
    if tmp_db.exists():
        tmp_db.unlink(missing_ok=True)
    conn = sqlite3.connect(str(tmp_db))
    cur = conn.cursor()
    cur.execute("PRAGMA synchronous = OFF")
    cur.execute("PRAGMA journal_mode = MEMORY")
    cur.execute(
        "CREATE TABLE aircraft "
        "(hex TEXT PRIMARY KEY, reg TEXT, type_code TEXT, description TEXT, flags INTEGER)"
    )

    batch = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f, delimiter=";")
        for row in reader:
            if len(row) < 3:
                continue
            hex_code = row[0].strip().upper()
            if not hex_code:
                continue
            reg       = row[1].strip() or None
            type_code = row[2].strip() or None
            desc      = (row[4].strip() if len(row) > 4 else "") or None
            flags     = _parse_db_flags(row[3]) if len(row) > 3 else None
            batch.append((hex_code, reg, type_code, desc, flags))
            if len(batch) >= 50000:
                cur.executemany("INSERT OR REPLACE INTO aircraft VALUES (?, ?, ?, ?, ?)", batch)
                batch = []
        if batch:
            cur.executemany("INSERT OR REPLACE INTO aircraft VALUES (?, ?, ?, ?, ?)", batch)
    cur.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
    conn.commit()
    conn.close()
    tmp_db.replace(db_path)


def _db_schema_version(db_path: Path) -> int:
    """The index's schema version, or 0 if it is missing or unreadable."""
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        try:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])
        finally:
            conn.close()
    except sqlite3.Error:
        return 0


def _load_db(csv_path: Path) -> dict[str, tuple[str | None, str | None, str | None, int | None]]:
    """Legacy dictionary loader for testing."""
    db: dict[str, tuple[str | None, str | None, str | None, int | None]] = {}
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f, delimiter=";")
        for row in reader:
            if len(row) < 3:
                continue
            hex_code = row[0].strip().upper()
            reg_raw  = row[1].strip() or None
            reg      = sys.intern(reg_raw) if reg_raw else None
            code_raw = row[2].strip() or None
            code     = sys.intern(code_raw) if code_raw else None
            desc_raw = (row[4].strip() if len(row) > 4 else "") or None
            desc     = sys.intern(desc_raw) if desc_raw else None
            flags    = _parse_db_flags(row[3]) if len(row) > 3 else None
            if hex_code:
                db[sys.intern(hex_code)] = (reg, code, desc, flags)
    return db


KEYS = {"type"}


def get(cfg: dict, ctx: ModuleContext) -> Tar1090DbEnricher:
    csv_path = ctx.module_dir / "aircraft.csv"
    db_path  = ctx.module_dir / "aircraft.db"

    if _needs_refresh(csv_path):
        try:
            _download(csv_path)
        except Exception as exc:
            if not csv_path.exists():
                print(f"  tar1090_db: download failed ({exc}), enrichment disabled")
                return Tar1090DbEnricher(db={})

    if csv_path.exists():
        stale_csv     = not db_path.exists() or db_path.stat().st_mtime < csv_path.stat().st_mtime
        stale_schema  = db_path.exists() and _db_schema_version(db_path) != _SCHEMA_VERSION
        if stale_csv or stale_schema:
            if stale_schema and not stale_csv:
                print(f"  tar1090_db: index schema is out of date — rebuilding {db_path.name} …")
            else:
                print(f"  tar1090_db: building SQLite index {db_path.name} …")
            _build_sqlite_db(csv_path, db_path)
        sqlite_db = SQLiteTarDb(db_path)
        print(f"  tar1090_db: active via SQLite (0 MB RAM overhead)")
        return Tar1090DbEnricher(sqlite_db)

    return Tar1090DbEnricher(db={})


