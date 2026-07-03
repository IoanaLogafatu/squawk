"""
plugins/tar1090_db.py

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

from __future__ import annotations

import csv
import gzip
import time
from pathlib import Path

import requests

from modules import BaseModule
from schemas.aircraft import Aircraft


_CSV_URL      = "https://github.com/wiedehopf/tar1090-db/raw/refs/heads/csv/aircraft.csv.gz"
_REFRESH_DAYS = 30


class Tar1090DbEnricher(BaseModule):

    def __init__(self, db: dict[str, tuple[str | None, str | None]]) -> None:
        # icao_hex (uppercase) → (registration, aircraft_type)
        self._db = db

    def process(self, aircraft: list[Aircraft]) -> list[Aircraft]:
        for a in aircraft:
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


def _load_db(csv_path: Path) -> dict[str, tuple[str | None, str | None]]:
    db: dict[str, tuple[str | None, str | None]] = {}
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f, delimiter=";")
        for row in reader:
            if len(row) < 3:
                continue
            hex_code = row[0].strip().upper()
            reg      = row[1].strip() or None
            # Prefer the human-readable description (col 4); fall back to the
            # ICAO type code (col 2) when the description field is empty.
            desc     = (row[4].strip() if len(row) > 4 else "") or row[2].strip() or None
            if hex_code:
                db[hex_code] = (reg, desc)
    return db


def get(cfg: dict) -> Tar1090DbEnricher:
    from config import config as squawk_config
    data_dir = Path(squawk_config.squawk.data_dir)
    csv_path = data_dir / "modules" / "tar1090_db" / "aircraft.csv"

    if _needs_refresh(csv_path):
        try:
            _download(csv_path)
        except Exception as exc:
            if csv_path.exists():
                print(f"  tar1090_db: refresh failed ({exc}), using cached data")
            else:
                print(f"  tar1090_db: download failed ({exc}), enrichment disabled")
                return Tar1090DbEnricher(db={})

    db = _load_db(csv_path)
    print(f"  tar1090_db: loaded {len(db):,} records")
    return Tar1090DbEnricher(db)
