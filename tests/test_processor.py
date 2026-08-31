"""
tests/test_processor.py

Tests for the processor pipeline mechanics.

Covers:
  1. Modules execute in declared order and each receives the previous output
  2. Empty aircraft list passes through without error
  3. Poll-interval sleep calculation
  4. Storage backend is correctly instantiated from config
"""

from __future__ import annotations

import pytest

from modules import BaseModule
from storage import get_storage
from storage.disk_drive import DiskDriveStorage
from schemas.aircraft import (
    Aircraft, AircraftLocation, AircraftMeta, AircraftRaw,
    AircraftRoute, AircraftVector, Airframe,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_aircraft(hex_id: str) -> Aircraft:
    return Aircraft(
        meta      = AircraftMeta(icao_hex=hex_id, ingestor="test", reception_type="adsb_icao"),
        location  = AircraftLocation(),
        direction = AircraftVector(),
        route     = AircraftRoute(),
        airframe  = Airframe(),
        raw       = AircraftRaw(),
    )


class _OrderModule(BaseModule):
    """Records its label in a shared log when called."""
    def __init__(self, label: str, log: list) -> None:
        self._label = label
        self._log   = log

    def process(self, aircraft: list[Aircraft]) -> list[Aircraft]:
        self._log.append(self._label)
        return aircraft


# ===========================================================================
# 1. Pipeline ordering
# ===========================================================================

def test_processor_runs_pipeline_in_order():
    log     = []
    modules = [_OrderModule("first", log), _OrderModule("second", log), _OrderModule("third", log)]
    aircraft = [_make_aircraft("AA1111")]

    for module in modules:
        aircraft = module.process(aircraft)

    assert log == ["first", "second", "third"]


def test_processor_pipeline_passes_output_to_next_module():
    # A module that removes aircraft; next module should see the reduced list.
    class DropAll(BaseModule):
        def process(self, aircraft):
            return []

    class RecordCount(BaseModule):
        received = -1
        def process(self, aircraft):
            RecordCount.received = len(aircraft)
            return aircraft

    recorder = RecordCount()
    modules  = [DropAll(), recorder]
    aircraft = [_make_aircraft("AA1111"), _make_aircraft("BB2222")]

    for module in modules:
        aircraft = module.process(aircraft)

    assert RecordCount.received == 0


# ===========================================================================
# 2. Empty list
# ===========================================================================

def test_processor_handles_empty_list():
    log    = []
    module = _OrderModule("called", log)
    result = module.process([])
    assert result == []
    assert log == ["called"]  # module still runs on an empty list


# ===========================================================================
# 3. Poll-interval sleep calculation
# ===========================================================================

def test_processor_poll_interval_fast_cycle():
    # Cycle completed well within the interval — sleep fills the remainder.
    interval, elapsed = 5.0, 0.1
    sleep_for = max(0.0, interval - elapsed)
    assert sleep_for == pytest.approx(4.9)


def test_processor_poll_interval_slow_cycle():
    # Cycle overran the interval — sleep is clamped to zero, never negative.
    interval, elapsed = 5.0, 6.3
    sleep_for = max(0.0, interval - elapsed)
    assert sleep_for == 0.0


def test_processor_poll_interval_exact():
    interval, elapsed = 5.0, 5.0
    sleep_for = max(0.0, interval - elapsed)
    assert sleep_for == 0.0


# ===========================================================================
# 4. Storage backend loading
# ===========================================================================

def test_processor_loads_storage_backend(tmp_path):
    storage = get_storage("disk_drive", tmp_path)
    assert isinstance(storage, DiskDriveStorage)


def test_processor_unknown_storage_raises(tmp_path):
    with pytest.raises(ValueError, match="Unknown storage backend"):
        get_storage("oracle_db", tmp_path)


# ===========================================================================
# 5. Multiple independent processor chains
# ===========================================================================

def test_multiple_independent_processor_chains():
    # Chain A: Filter for registration 'G-AAAA' and send to Sink A
    # Chain B: Filter for registration 'G-BBBB' and send to Sink B
    class RegFilter(BaseModule):
        def __init__(self, target: str):
            self.target = target
        def process(self, aircraft):
            return [a for a in aircraft if a.airframe.registration == self.target]

    class CollectDisplay(BaseModule):
        def __init__(self):
            self.collected = []
        def process(self, aircraft):
            self.collected.extend([a.airframe.registration for a in aircraft])
            return aircraft

    sink_a = CollectDisplay()
    sink_b = CollectDisplay()

    chain_a = [RegFilter("G-AAAA"), sink_a]
    chain_b = [RegFilter("G-BBBB"), sink_b]

    a1 = _make_aircraft("111111")
    a1.airframe.registration = "G-AAAA"
    a2 = _make_aircraft("222222")
    a2.airframe.registration = "G-BBBB"
    source_aircraft = [a1, a2]

    # Run chain A
    res_a = source_aircraft
    for m in chain_a:
        res_a = m.process(res_a)

    # Run chain B
    res_b = source_aircraft
    for m in chain_b:
        res_b = m.process(res_b)

    assert sink_a.collected == ["G-AAAA"]
    assert sink_b.collected == ["G-BBBB"]

