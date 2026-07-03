"""
tests/test_snapshot.py

Tests for the disk_drive storage backend.

Covers:
  1. Initialisation   (aircraft_dir created)
  2. Save             (files written, valid JSON, upsert-if-newer)
  3. Expiry           (stale files deleted by save, fresh files kept)
  4. Retrieve         (array and single, staleness filtering)
"""

import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from storage.disk_drive import DiskDriveStorage
from storage import STALE_SECONDS
from schemas.aircraft import (
    Aircraft,
    AircraftLocation,
    AircraftMeta,
    AircraftRaw,
    AircraftRoute,
    AircraftVector,
    Airframe,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _storage(tmp_path: Path) -> DiskDriveStorage:
    return DiskDriveStorage(tmp_path)


def _make(hex_id: str, observed_at: datetime | None = None) -> Aircraft:
    return Aircraft(
        meta      = AircraftMeta(icao_hex=hex_id, ingestor="test", observed_at=observed_at),
        location  = AircraftLocation(seen_seconds=0.0),
        direction = AircraftVector(),
        route     = AircraftRoute(),
        airframe  = Airframe(),
        raw       = AircraftRaw(),
    )


def _age(path: Path, seconds: float) -> None:
    """Set a file's mtime to `seconds` in the past."""
    old = time.time() - seconds
    os.utime(path, (old, old))


def _aircraft_path(tmp_path: Path, hex_id: str) -> Path:
    return tmp_path / "tracked_aircraft" / f"{hex_id}.json"


# ===========================================================================
# 1. Initialisation
# ===========================================================================

def test_aircraft_dir_created(tmp_path):
    _storage(tmp_path)
    assert (tmp_path / "tracked_aircraft").is_dir()


# ===========================================================================
# 2. Save
# ===========================================================================

def test_save_creates_aircraft_files(tmp_path):
    _storage(tmp_path).save_aircraft_array([_make("AA1111"), _make("BB2222")])
    assert _aircraft_path(tmp_path, "AA1111").exists()
    assert _aircraft_path(tmp_path, "BB2222").exists()


def test_aircraft_file_is_valid_json(tmp_path):
    _storage(tmp_path).save_aircraft_array([_make("AA1111")])
    data = json.loads(_aircraft_path(tmp_path, "AA1111").read_text())
    assert isinstance(data, dict)


def test_aircraft_file_contains_icao_hex(tmp_path):
    _storage(tmp_path).save_aircraft_array([_make("AA1111")])
    data = json.loads(_aircraft_path(tmp_path, "AA1111").read_text())
    assert data["meta"]["icao_hex"] == "AA1111"


def test_no_tmp_files_after_save(tmp_path):
    _storage(tmp_path).save_aircraft_array([_make("AA1111"), _make("BB2222")])
    assert list(tmp_path.rglob("*.tmp")) == []


def test_upsert_newer_record_wins(tmp_path):
    s  = _storage(tmp_path)
    t0 = datetime(2026, 5, 12, 10, 0, 0, tzinfo=timezone.utc)
    s.save_aircraft_array([_make("AA1111", observed_at=t0)])
    s.save_aircraft_array([_make("AA1111", observed_at=t0 + timedelta(minutes=1))])
    data = json.loads(_aircraft_path(tmp_path, "AA1111").read_text())
    assert "10:01:00" in data["meta"]["observed_at"]


def test_upsert_stale_record_rejected(tmp_path):
    s  = _storage(tmp_path)
    t0 = datetime(2026, 5, 12, 10, 0, 0, tzinfo=timezone.utc)
    s.save_aircraft_array([_make("AA1111", observed_at=t0)])
    s.save_aircraft_array([_make("AA1111", observed_at=t0 - timedelta(minutes=1))])
    data = json.loads(_aircraft_path(tmp_path, "AA1111").read_text())
    assert "10:00:00" in data["meta"]["observed_at"]


# ===========================================================================
# 3. Expiry
# ===========================================================================

def test_stale_file_expired_on_next_save(tmp_path):
    s = _storage(tmp_path)
    s.save_aircraft_array([_make("AA1111")])
    _age(_aircraft_path(tmp_path, "AA1111"), STALE_SECONDS + 10)
    s.save_aircraft_array([_make("BB2222")])   # triggers _expire_stale
    assert not _aircraft_path(tmp_path, "AA1111").exists()


def test_fresh_file_not_expired(tmp_path):
    s = _storage(tmp_path)
    s.save_aircraft_array([_make("AA1111"), _make("BB2222")])
    s.save_aircraft_array([_make("AA1111")])   # BB2222 absent but still fresh
    assert _aircraft_path(tmp_path, "BB2222").exists()


def test_live_aircraft_survives_expiry(tmp_path):
    # AA1111 file is aged, but it's in the new write — save writes first,
    # refreshing mtime, so _expire_stale leaves it alone.
    s = _storage(tmp_path)
    s.save_aircraft_array([_make("AA1111")])
    _age(_aircraft_path(tmp_path, "AA1111"), STALE_SECONDS + 10)
    s.save_aircraft_array([_make("AA1111")])
    assert _aircraft_path(tmp_path, "AA1111").exists()


# ===========================================================================
# 4. Retrieve
# ===========================================================================

def test_retrieve_array_returns_fresh_aircraft(tmp_path):
    s = _storage(tmp_path)
    s.save_aircraft_array([_make("AA1111"), _make("BB2222")])
    result = s.retrieve_aircraft_array()
    hexes = {d["meta"]["icao_hex"] for d in result}
    assert hexes == {"AA1111", "BB2222"}


def test_retrieve_array_excludes_stale(tmp_path):
    s = _storage(tmp_path)
    s.save_aircraft_array([_make("AA1111"), _make("BB2222")])
    _age(_aircraft_path(tmp_path, "BB2222"), STALE_SECONDS + 10)
    result = s.retrieve_aircraft_array()
    hexes = {d["meta"]["icao_hex"] for d in result}
    assert "AA1111" in hexes
    assert "BB2222" not in hexes


def test_retrieve_aircraft_returns_record(tmp_path):
    s = _storage(tmp_path)
    s.save_aircraft_array([_make("AA1111")])
    result = s.retrieve_aircraft("AA1111")
    assert result is not None
    assert result["meta"]["icao_hex"] == "AA1111"


def test_retrieve_aircraft_stale_returns_none(tmp_path):
    s = _storage(tmp_path)
    s.save_aircraft_array([_make("AA1111")])
    _age(_aircraft_path(tmp_path, "AA1111"), STALE_SECONDS + 10)
    assert s.retrieve_aircraft("AA1111") is None


def test_retrieve_aircraft_missing_returns_none(tmp_path):
    assert _storage(tmp_path).retrieve_aircraft("FFFFFF") is None


def test_list_hex_ids_excludes_stale(tmp_path):
    s = _storage(tmp_path)
    s.save_aircraft_array([_make("AA1111"), _make("BB2222")])
    _age(_aircraft_path(tmp_path, "BB2222"), STALE_SECONDS + 10)
    assert s.list_aircraft_hex_ids() == ["AA1111"]


# ---------------------------------------------------------------------------
# Race condition — file deleted between glob() and stat()
# ---------------------------------------------------------------------------

def test_retrieve_array_survives_file_deleted_after_glob(tmp_path, monkeypatch):
    """Regression: FileNotFoundError from stat() crashed the Processor thread."""
    s = _storage(tmp_path)
    s.save_aircraft_array([_make("AA1111")])

    original_stat = Path.stat
    def stat_raises(self, **kw):
        if self.parent == s.aircraft_dir and self.suffix == ".json":
            raise FileNotFoundError(f"simulated race: {self}")
        return original_stat(self, **kw)

    monkeypatch.setattr(Path, "stat", stat_raises)
    assert s.retrieve_aircraft_array() == []


def test_list_hex_ids_survives_file_deleted_after_glob(tmp_path, monkeypatch):
    s = _storage(tmp_path)
    s.save_aircraft_array([_make("AA1111")])

    original_stat = Path.stat
    def stat_raises(self, **kw):
        if self.parent == s.aircraft_dir and self.suffix == ".json":
            raise FileNotFoundError(f"simulated race: {self}")
        return original_stat(self, **kw)

    monkeypatch.setattr(Path, "stat", stat_raises)
    assert s.list_aircraft_hex_ids() == []
