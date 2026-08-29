"""
tests/test_ingest_modules.py

Tests for ingest-time module wiring:
  1. get_ingest_modules helper builds modules from an ingestor config block
  2. modules run before storage (enrichment is baked into what's saved)
  3. an empty / missing modules list is a no-op
  4. a filter at ingest narrows what reaches storage
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from ingestor import get_ingest_modules
from ingestor.personal_adsb import ingestor as personal_ingestor
from modules import BaseModule
from schemas.aircraft import Aircraft


# ---------------------------------------------------------------------------
# 1. Helper
# ---------------------------------------------------------------------------

def test_get_ingest_modules_builds_named_modules():
    result = get_ingest_modules({"modules": ["pass_through"]})
    assert len(result) == 1
    assert isinstance(result[0], BaseModule)


def test_get_ingest_modules_empty_when_no_key():
    assert get_ingest_modules({}) == []
    assert get_ingest_modules({"modules": []}) == []


# ---------------------------------------------------------------------------
# Helpers to drive one poll cycle of the personal_adsb ingestor
# ---------------------------------------------------------------------------

def _snapshot(now: float) -> dict:
    return {
        "now": now,
        "aircraft": [
            {"hex": "aaaa01", "seen": 0.5, "flight": "RYR1234 ",
             "lat": 52.0, "lon": -1.0, "alt_baro": 12000, "gs": 400},
            {"hex": "bbbb02", "seen": 1.0, "flight": "EZY9999 ",
             "lat": 52.1, "lon": -1.1, "alt_baro": 20000, "gs": 420},
        ],
    }


class _MarkerModule(BaseModule):
    """Enrichment stand-in: writes a marker into airframe.operator."""
    def process(self, aircraft: list[Aircraft]) -> list[Aircraft]:
        for a in aircraft:
            a.airframe.operator = "MARKER"
        return aircraft


class _KeepOne(BaseModule):
    """Filter stand-in: only keeps a single hex."""
    def __init__(self, keep_hex: str) -> None:
        self._keep = keep_hex.upper()

    def process(self, aircraft: list[Aircraft]) -> list[Aircraft]:
        return [a for a in aircraft if a.meta.icao_hex == self._keep]


def _run_one_cycle(monkeypatch, tmp_path, ingest_modules):
    """Drive personal_adsb.run() through exactly one poll cycle."""
    from storage.disk_drive import DiskDriveStorage
    import storage as storage_module

    # Point storage at tmp_path via the shared factory, then override the
    # config-driven get_storage call to hand back this concrete backend.
    storage_module._INSTANCES.clear()
    backend = DiskDriveStorage(tmp_path)

    class _Cfg:
        class squawk: data_dir = str(tmp_path)
        class storage: method = "disk_drive"
        ingestors = {"personal_adsb": {
            "enabled": True,
            "poll_interval_seconds": 5,
            "timeout_seconds": 1,
            "receivers": [{"name": "r1", "url": "http://ignored/"}],
        }}
        modules = {}

    monkeypatch.setattr(personal_ingestor, "config", _Cfg)
    monkeypatch.setattr("storage.get_storage", lambda method, data_dir: backend)
    monkeypatch.setattr("ingestor.get_ingest_modules", lambda cfg: ingest_modules)

    monkeypatch.setattr(
        personal_ingestor,
        "_fetch_snapshot",
        lambda url, timeout: (_snapshot(datetime.now(timezone.utc).timestamp()), None),
    )

    # Break out after one cycle by making time.sleep raise.
    class _StopAfterOneCycle(Exception): ...
    def _stop(_seconds):
        raise _StopAfterOneCycle
    monkeypatch.setattr(personal_ingestor.time, "sleep", _stop)

    with pytest.raises(_StopAfterOneCycle):
        personal_ingestor.run()

    return backend


# ---------------------------------------------------------------------------
# 2. Enrichment runs before save — visible in the JSON on disk
# ---------------------------------------------------------------------------

def test_ingest_module_enrichment_is_persisted(monkeypatch, tmp_path):
    _run_one_cycle(monkeypatch, tmp_path, [_MarkerModule()])

    files = list((tmp_path / "tracked_aircraft").glob("*.json"))
    assert len(files) == 2
    for path in files:
        data = json.loads(path.read_text())
        assert data["airframe"]["operator"] == "MARKER"


# ---------------------------------------------------------------------------
# 3. Empty modules list — records saved unchanged
# ---------------------------------------------------------------------------

def test_ingest_no_modules_preserves_records(monkeypatch, tmp_path):
    _run_one_cycle(monkeypatch, tmp_path, [])

    files = list((tmp_path / "tracked_aircraft").glob("*.json"))
    assert len(files) == 2
    for path in files:
        data = json.loads(path.read_text())
        assert data["airframe"]["operator"] is None


# ---------------------------------------------------------------------------
# 4. Filter at ingest narrows what reaches storage
# ---------------------------------------------------------------------------

def test_ingest_filter_narrows_storage(monkeypatch, tmp_path):
    _run_one_cycle(monkeypatch, tmp_path, [_KeepOne("AAAA01")])

    stored = sorted(p.stem for p in (tmp_path / "tracked_aircraft").glob("*.json"))
    assert stored == ["AAAA01"]
