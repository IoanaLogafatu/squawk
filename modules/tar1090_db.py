"""
modules/tar1090_db.py

Enriches Aircraft records with registration and aircraft type from the
tar1090 aircraft database (aircraft.csv), a semicolon-delimited CSV.

Only fills fields that are currently UNKNOWN (None) — never overwrites
data already supplied by the source.

CSV format (no header, semicolon-delimited, 8 fields):
    hex ; registration ; type_code ; flags ; description ; ... (remaining unused)

The CSV is downloaded automatically from the tar1090-db GitHub release if it
is missing or older than 30 days, then cached at:
    <data_dir>/modules/tar1090_db/aircraft.csv
"""

import csv
import gzip
import sqlite3
import sys
import threading
import time
from pathlib import Path

import requests

from modules import BaseModule
from schemas.aircraft import Aircraft


_CSV_URL      = "https://github.com/wiedehopf/tar1090-db/raw/refs/heads/csv/aircraft.csv.gz"
_REFRESH_DAYS = 30


class SQLiteTarDb:

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._local = threading.local()

    def get(self, hex_code: str) -> tuple[str | None, str | None] | None:
        if not hasattr(self._local, "conn"):
            self._local.conn = sqlite3.connect(str(self._db_path), timeout=5)
            self._local.cursor = self._local.conn.cursor()
        self._local.cursor.execute("SELECT reg, type_code FROM aircraft WHERE hex = ?", (hex_code,))
        return self._local.cursor.fetchone()


class Tar1090DbEnricher(BaseModule):

    def __init__(self, db: dict[str, tuple[str | None, str | None]] | SQLiteTarDb | None = None) -> None:
        # icao_hex (uppercase) → (registration, aircraft_type)
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
            reg, type_code = row
            if a.airframe.registration is None and reg:
                a.airframe.registration = reg
            if a.airframe.aircraft_type is None and type_code:
                a.airframe.aircraft_type = type_code
        return aircraft


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
    cur.execute("CREATE TABLE aircraft (hex TEXT PRIMARY KEY, reg TEXT, type_code TEXT)")

    batch = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f, delimiter=";")
        for row in reader:
            if len(row) < 3:
                continue
            hex_code = row[0].strip().upper()
            if not hex_code:
                continue
            reg = row[1].strip() or None
            desc = (row[4].strip() if len(row) > 4 else "") or row[2].strip() or None
            batch.append((hex_code, reg, desc))
            if len(batch) >= 50000:
                cur.executemany("INSERT OR REPLACE INTO aircraft VALUES (?, ?, ?)", batch)
                batch = []
        if batch:
            cur.executemany("INSERT OR REPLACE INTO aircraft VALUES (?, ?, ?)", batch)
    conn.commit()
    conn.close()
    tmp_db.replace(db_path)


def _load_db(csv_path: Path) -> dict[str, tuple[str | None, str | None]]:
    """Legacy dictionary loader for testing."""
    db: dict[str, tuple[str | None, str | None]] = {}
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f, delimiter=";")
        for row in reader:
            if len(row) < 3:
                continue
            hex_code = row[0].strip().upper()
            reg_raw  = row[1].strip() or None
            reg      = sys.intern(reg_raw) if reg_raw else None
            desc_raw = (row[4].strip() if len(row) > 4 else "") or row[2].strip() or None
            desc     = sys.intern(desc_raw) if desc_raw else None
            if hex_code:
                db[sys.intern(hex_code)] = (reg, desc)
    return db


_SQLITE_INSTANCE: SQLiteTarDb | None = None
_SQLITE_LOCK = threading.Lock()


def get(cfg: dict) -> Tar1090DbEnricher:
    global _SQLITE_INSTANCE
    with _SQLITE_LOCK:
        if _SQLITE_INSTANCE is None:
            from config import config as squawk_config
            data_dir = Path(squawk_config.squawk.data_dir)
            csv_path = data_dir / "modules" / "tar1090_db" / "aircraft.csv"
            db_path  = data_dir / "modules" / "tar1090_db" / "aircraft.db"

            if _needs_refresh(csv_path):
                try:
                    _download(csv_path)
                except Exception as exc:
                    if not csv_path.exists():
                        print(f"  tar1090_db: download failed ({exc}), enrichment disabled")
                        return Tar1090DbEnricher(db={})

            if csv_path.exists():
                if not db_path.exists() or db_path.stat().st_mtime < csv_path.stat().st_mtime:
                    print(f"  tar1090_db: building SQLite index {db_path.name} …")
                    _build_sqlite_db(csv_path, db_path)
                _SQLITE_INSTANCE = SQLiteTarDb(db_path)
                print(f"  tar1090_db: active via SQLite (0 MB RAM overhead)")
            else:
                return Tar1090DbEnricher(db={})

    return Tar1090DbEnricher(_SQLITE_INSTANCE)


