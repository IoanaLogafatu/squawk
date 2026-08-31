"""
tests/test_storage_shared.py

Covers the shared-instance factory in storage/__init__.py and the
TTL-cached snapshot on BaseStorage.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

import storage as storage_module
from storage import SNAPSHOT_TTL_SECONDS, get_storage
from storage.disk_drive import DiskDriveStorage
from schemas.aircraft import (
    Aircraft, AircraftLocation, AircraftMeta, AircraftRaw,
    AircraftRoute, AircraftVector, Airframe,
)


@pytest.fixture(autouse=True)
def reset_storage_instances():
    saved = storage_module._INSTANCES.copy()
    storage_module._INSTANCES.clear()
    yield
    storage_module._INSTANCES.clear()
    storage_module._INSTANCES.update(saved)


def _make(hex_id: str) -> Aircraft:
    return Aircraft(
        meta      = AircraftMeta(
            icao_hex=hex_id,
            ingestor="test",
            reception_type="adsb_icao",
            observed_at=datetime.now(timezone.utc),
        ),
        location  = AircraftLocation(seen_seconds=0.0),
        direction = AircraftVector(),
        route     = AircraftRoute(),
        airframe  = Airframe(),
        raw       = AircraftRaw(),
    )


# ---------------------------------------------------------------------------
# 1. Shared instance
# ---------------------------------------------------------------------------

def test_get_storage_returns_same_instance_for_same_dir(tmp_path):
    first  = get_storage("disk_drive", tmp_path)
    second = get_storage("disk_drive", tmp_path)
    assert first is second


def test_get_storage_returns_different_instance_for_different_dir(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    first  = get_storage("disk_drive", tmp_path)
    second = get_storage("disk_drive", other)
    assert first is not second


# ---------------------------------------------------------------------------
# 2. Unknown backend raises every call
# ---------------------------------------------------------------------------

def test_get_storage_unknown_backend_raises_first_call(tmp_path):
    with pytest.raises(ValueError, match="Unknown storage backend"):
        get_storage("oracle_db", tmp_path)


def test_get_storage_unknown_backend_raises_repeatedly(tmp_path):
    with pytest.raises(ValueError):
        get_storage("oracle_db", tmp_path)
    with pytest.raises(ValueError, match="Unknown storage backend"):
        get_storage("oracle_db", tmp_path)


# ---------------------------------------------------------------------------
# 3. Snapshot is TTL-cached
# ---------------------------------------------------------------------------

def test_snapshot_cached_within_ttl_and_refreshes_after(tmp_path):
    storage = DiskDriveStorage(tmp_path)
    storage.save_aircraft_array([_make("AAAA01"), _make("AAAA02")])

    first = storage.retrieve_aircraft_objects()
    assert len(first) == 2

    storage.save_aircraft_array([_make("AAAA03")])
    second = storage.retrieve_aircraft_objects()
    assert len(second) == 2  # TTL still valid; new file not yet reflected

    storage._snapshot_at = 0.0
    third = storage.retrieve_aircraft_objects()
    assert len(third) == 3


# ---------------------------------------------------------------------------
# 4. Objects shared, list is not
# ---------------------------------------------------------------------------

def test_snapshot_shares_objects_but_returns_fresh_list(tmp_path):
    storage = DiskDriveStorage(tmp_path)
    storage.save_aircraft_array([_make("BBBB01"), _make("BBBB02")])

    first  = storage.retrieve_aircraft_objects()
    second = storage.retrieve_aircraft_objects()

    assert first is not second
    assert len(first) == len(second) == 2
    for a, b in zip(first, second):
        assert a is b

    first.clear()
    third = storage.retrieve_aircraft_objects()
    assert len(third) == 2


# ---------------------------------------------------------------------------
# 5. Concurrent refresh reads once
# ---------------------------------------------------------------------------

def test_concurrent_refresh_reads_underlying_once(tmp_path, monkeypatch):
    storage = DiskDriveStorage(tmp_path)
    storage.save_aircraft_array([_make("CCCC01"), _make("CCCC02")])
    storage._snapshot = []
    storage._snapshot_at = 0.0

    call_count = 0
    real_read = storage.retrieve_aircraft_array

    def counting_read():
        nonlocal call_count
        call_count += 1
        time.sleep(0.05)  # widen the concurrency window
        return real_read()

    monkeypatch.setattr(storage, "retrieve_aircraft_array", counting_read)

    barrier = threading.Barrier(10)
    results: list[list] = []
    results_lock = threading.Lock()

    def worker():
        barrier.wait()
        r = storage.retrieve_aircraft_objects()
        with results_lock:
            results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert call_count == 1
    assert len(results) == 10
    for r in results:
        assert len(r) == 2
