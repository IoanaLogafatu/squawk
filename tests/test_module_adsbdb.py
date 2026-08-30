"""
tests/test_module_adsbdb.py

Tests for the adsbdb enrichment module.

No real network calls — requests.get is monkeypatched throughout.

Covers:
  1.  Skip when callsign is UNKNOWN
  2.  Cache hit, fresh — no HTTP call
  3.  Cache miss → fetch → write cache file + populate fields
  4.  Cache stale → fetch re-triggered
  5.  Stale + fetch fails → stale data applied
  6.  404 → not_found marker written, fields stay UNKNOWN
  7.  404 marker honoured — no HTTP call
  8.  UNKNOWN-only writes — pre-set fields not overwritten; raw.adsbdb always overwritten
  9.  Rate limit honoured — no API call, fields UNKNOWN
  10. Callsign normalisation — trailing space trimmed, filename uppercased
  13. In-memory memo — concurrent stampede prevention (brief-adsbdb-stampede.md)
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from modules import clear_module_pool, get_module
from modules.adsbdb import AdsbdbEnricher, _MEMO_TTL_SECONDS, _RATE_60S
from schemas.aircraft import (
    Aircraft, AircraftLocation, AircraftMeta, AircraftRaw,
    AircraftRoute, AircraftVector, Airframe,
)


FIXTURES = Path(__file__).parent / "fixtures"

# The fixture file mirrors a real adsbdb HTTP response.
_FULL_API_RESPONSE = json.loads((FIXTURES / "4D2387 response.json").read_text())
# The module stores / applies only the inner "response" object.
_API_INNER = _FULL_API_RESPONSE["response"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_aircraft(callsign=None, operator=None) -> Aircraft:
    return Aircraft(
        meta      = AircraftMeta(icao_hex="4D2387", ingestor="test", reception_type="mlat"),
        location  = AircraftLocation(),
        direction = AircraftVector(),
        route     = AircraftRoute(callsign=callsign),
        airframe  = Airframe(operator=operator),
        raw       = AircraftRaw(),
    )


def _mock_200(monkeypatch) -> None:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = _FULL_API_RESPONSE
    monkeypatch.setattr("modules.adsbdb.requests.get", lambda *a, **kw: resp)


def _mock_404(monkeypatch) -> None:
    resp = MagicMock()
    resp.status_code = 404
    monkeypatch.setattr("modules.adsbdb.requests.get", lambda *a, **kw: resp)


def _mock_error(monkeypatch) -> None:
    def _raise(*a, **kw):
        raise ConnectionError("no network")
    monkeypatch.setattr("modules.adsbdb.requests.get", _raise)


# ---------------------------------------------------------------------------
# 1. Enrich by hex when callsign is UNKNOWN
# ---------------------------------------------------------------------------

def test_enrich_when_callsign_unknown_fetches_by_hex(tmp_path, monkeypatch):
    _mock_200(monkeypatch)
    enricher = AdsbdbEnricher(cache_dir=tmp_path)
    a = _make_aircraft(callsign=None)
    enricher.process([a])
    assert a.airframe.manufacturer == "Boeing"
    assert a.airframe.registration == "9H-VUZ"
    assert (tmp_path / "4D2387.json").exists()


def test_fallback_to_hex_when_callsign_invalid(tmp_path, monkeypatch):
    calls = []
    def mock_get(url, **kw):
        calls.append(url)
        resp = MagicMock()
        resp.status_code = 200
        if "callsign=" in url:
            resp.json.return_value = {"response": "invalid callsign: UNKNOWN123"}
        else:
            resp.json.return_value = _FULL_API_RESPONSE
        return resp

    monkeypatch.setattr("modules.adsbdb.requests.get", mock_get)
    enricher = AdsbdbEnricher(cache_dir=tmp_path)
    a = _make_aircraft(callsign="UNKNOWN123")
    enricher.process([a])

    assert len(calls) == 2
    assert "callsign=UNKNOWN123" in calls[0]
    assert "callsign=" not in calls[1]
    assert a.airframe.registration == "9H-VUZ"
    assert a.airframe.manufacturer == "Boeing"


# ---------------------------------------------------------------------------
# 2. Cache hit, fresh — no HTTP call
# ---------------------------------------------------------------------------

def test_cache_hit_fresh_no_http_call(tmp_path, monkeypatch):
    (tmp_path / "RYR54NN.json").write_text(
        json.dumps(_API_INNER), encoding="utf-8"
    )
    called = []
    monkeypatch.setattr("modules.adsbdb.requests.get", lambda *a, **kw: called.append(1))
    enricher = AdsbdbEnricher(cache_dir=tmp_path)
    enricher.process([_make_aircraft(callsign="RYR54NN")])
    assert not called


# ---------------------------------------------------------------------------
# 3. Cache miss → fetch → write cache file + populate fields
# ---------------------------------------------------------------------------

def test_cache_miss_fetch_writes_cache_and_populates_fields(tmp_path, monkeypatch):
    _mock_200(monkeypatch)
    enricher = AdsbdbEnricher(cache_dir=tmp_path)
    a = _make_aircraft(callsign="RYR54NN")
    enricher.process([a])

    assert (tmp_path / "RYR54NN.json").exists()

    assert a.airframe.manufacturer     == "Boeing"
    assert a.airframe.registration     == "9H-VUZ"
    assert a.airframe.aircraft_type    == "737MAX 8 200"
    assert a.airframe.operator         == "Malta Air"
    assert a.route.origin_iata         == "REU"
    assert a.route.origin_name         == "Reus Airport"
    assert a.route.origin_country      == "Spain"
    assert a.route.destination_iata    == "LBA"
    assert a.route.destination_name    == "Leeds Bradford Airport"
    assert a.route.destination_country == "United Kingdom"
    assert a.route.airline_name        == "Ryanair"
    assert a.route.airline_country     == "Ireland"
    assert a.raw.adsbdb                == _API_INNER


# ---------------------------------------------------------------------------
# 4. Cache stale → fetch re-triggered
# ---------------------------------------------------------------------------

def test_cache_stale_triggers_fetch(tmp_path, monkeypatch):
    cache_file = tmp_path / "RYR54NN.json"
    cache_file.write_text(json.dumps(_API_INNER), encoding="utf-8")
    old_time = time.time() - 7200   # 2 hours ago
    os.utime(cache_file, (old_time, old_time))

    fetch_calls = []

    def mock_get(url, **kw):
        fetch_calls.append(url)
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = _FULL_API_RESPONSE
        return resp

    monkeypatch.setattr("modules.adsbdb.requests.get", mock_get)
    enricher = AdsbdbEnricher(cache_dir=tmp_path)
    enricher.process([_make_aircraft(callsign="RYR54NN")])
    assert len(fetch_calls) == 1


# ---------------------------------------------------------------------------
# 5. Stale + fetch fails → stale data applied
# ---------------------------------------------------------------------------

def test_stale_fetch_fails_uses_stale_data(tmp_path, monkeypatch):
    cache_file = tmp_path / "RYR54NN.json"
    cache_file.write_text(json.dumps(_API_INNER), encoding="utf-8")
    old_time = time.time() - 7200
    os.utime(cache_file, (old_time, old_time))

    _mock_error(monkeypatch)
    enricher = AdsbdbEnricher(cache_dir=tmp_path)
    a = _make_aircraft(callsign="RYR54NN")
    enricher.process([a])

    assert a.airframe.manufacturer == "Boeing"
    assert a.raw.adsbdb            == _API_INNER


# ---------------------------------------------------------------------------
# 6. 404 → not_found marker written, fields stay UNKNOWN
# ---------------------------------------------------------------------------

def test_404_writes_not_found_marker_and_fields_stay_unknown(tmp_path, monkeypatch):
    _mock_404(monkeypatch)
    enricher = AdsbdbEnricher(cache_dir=tmp_path)
    a = _make_aircraft(callsign="RYR54NN")
    enricher.process([a])

    cache_file = tmp_path / "RYR54NN.json"
    assert cache_file.exists()
    marker = json.loads(cache_file.read_text())
    assert marker.get("not_found") is True
    assert "checked_at" in marker

    assert a.airframe.manufacturer is None
    assert a.route.origin_iata     is None
    assert a.raw.adsbdb            == {}


# ---------------------------------------------------------------------------
# 7. Fresh 404 marker honoured — no HTTP call
# ---------------------------------------------------------------------------

def test_fresh_not_found_marker_prevents_http_call(tmp_path, monkeypatch):
    (tmp_path / "RYR54NN.json").write_text(
        json.dumps({"not_found": True, "checked_at": "2026-05-13T12:00:00+00:00"}),
        encoding="utf-8",
    )
    called = []
    monkeypatch.setattr("modules.adsbdb.requests.get", lambda *a, **kw: called.append(1))
    enricher = AdsbdbEnricher(cache_dir=tmp_path)
    enricher.process([_make_aircraft(callsign="RYR54NN")])
    assert not called


# ---------------------------------------------------------------------------
# 8. UNKNOWN-only writes — pre-set fields not overwritten; raw.adsbdb always overwritten
# ---------------------------------------------------------------------------

def test_unknown_only_does_not_overwrite_preset_operator(tmp_path, monkeypatch):
    _mock_200(monkeypatch)
    enricher = AdsbdbEnricher(cache_dir=tmp_path)
    a = _make_aircraft(callsign="RYR54NN", operator="PreSet")
    enricher.process([a])

    assert a.airframe.operator == "PreSet"    # not overwritten
    assert a.raw.adsbdb        == _API_INNER  # always overwritten


# ---------------------------------------------------------------------------
# 9. Rate limit honoured — no API call, fields UNKNOWN
# ---------------------------------------------------------------------------

def test_rate_limit_skips_api_call(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr("modules.adsbdb.requests.get", lambda *a, **kw: called.append(1))
    enricher = AdsbdbEnricher(cache_dir=tmp_path)

    now = time.monotonic()
    for _ in range(512):
        enricher._call_times.append(now - 1)   # all within last 60 s

    a = _make_aircraft(callsign="RYR54NN")
    enricher.process([a])

    assert not called
    assert a.airframe.manufacturer is None


# ---------------------------------------------------------------------------
# 10. Callsign normalisation — trailing space trimmed, filename uppercased
# ---------------------------------------------------------------------------

def test_callsign_normalised_to_trimmed_uppercase_filename(tmp_path, monkeypatch):
    _mock_200(monkeypatch)
    enricher = AdsbdbEnricher(cache_dir=tmp_path)
    enricher.process([_make_aircraft(callsign="RYR54NN ")])   # trailing space

    assert     (tmp_path / "RYR54NN.json").exists()
    assert not (tmp_path / "RYR54NN .json").exists()


# ---------------------------------------------------------------------------
# 11. Shared instance — the factory pools by (name, cfg), not the module
# ---------------------------------------------------------------------------

@pytest.fixture
def reset_module_pool(tmp_path, monkeypatch):
    from config import config as squawk_config
    clear_module_pool()
    monkeypatch.setattr(squawk_config.squawk, "data_dir", str(tmp_path))
    yield
    clear_module_pool()


def test_get_returns_shared_instance(reset_module_pool):
    first  = get_module("adsbdb", {})
    second = get_module("adsbdb", {})
    assert first is second


def test_eight_chains_referencing_adsbdb_share_one_instance(reset_module_pool):
    # Mirrors the TV-wall config: eight processor chains all naming "adsbdb".
    instances = [get_module("adsbdb", {}) for _ in range(8)]
    assert len({id(i) for i in instances}) == 1


def test_rate_limiter_is_shared_across_get_calls(reset_module_pool):
    first  = get_module("adsbdb", {})
    second = get_module("adsbdb", {})
    now = time.monotonic()
    for _ in range(_RATE_60S):
        first._call_times.append(now - 1)
    assert second._try_acquire() is False


# ---------------------------------------------------------------------------
# 12. One permit per fetch — guard lives in _fetch, not the caller
# ---------------------------------------------------------------------------

def test_fetch_records_one_permit_per_call(tmp_path, monkeypatch):
    _mock_error(monkeypatch)
    enricher = AdsbdbEnricher(cache_dir=tmp_path)
    enricher._fetch("4D2387", "RYR54NN")
    enricher._fetch("4D2387", None)
    assert len(enricher._call_times) == 2


def test_fetch_denied_when_rate_limited_and_no_http(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr("modules.adsbdb.requests.get", lambda *a, **kw: called.append(1))
    enricher = AdsbdbEnricher(cache_dir=tmp_path)
    now = time.monotonic()
    for _ in range(_RATE_60S):
        enricher._call_times.append(now - 1)

    assert enricher._fetch("4D2387", "RYR54NN") is None
    assert not called


# ---------------------------------------------------------------------------
# 13. In-memory memo — concurrent stampede prevention
# ---------------------------------------------------------------------------

def test_concurrent_lookups_produce_one_fetch(tmp_path, monkeypatch):
    calls = []
    call_lock = threading.Lock()

    def fake_fetch(hex_id, callsign=None):
        with call_lock:
            calls.append((hex_id, callsign))
        time.sleep(0.05)
        return {"aircraft": {"manufacturer": "Boeing"}}

    enricher = AdsbdbEnricher(cache_dir=tmp_path)
    monkeypatch.setattr(enricher, "_fetch", fake_fetch)

    results: list = [None, None, None]

    def worker(i: int) -> None:
        results[i] = enricher._get("40097D", "EZY123")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(calls) == 1
    assert all(r == results[0] for r in results)


def test_memo_hit_avoids_disk(tmp_path, monkeypatch):
    _mock_200(monkeypatch)
    enricher = AdsbdbEnricher(cache_dir=tmp_path)

    first = enricher._get("4D2387", None)
    assert first is not None
    cache_file = tmp_path / "4D2387.json"
    assert cache_file.exists()
    cache_file.unlink()

    calls = []
    monkeypatch.setattr(enricher, "_fetch", lambda *a, **kw: calls.append(1))

    second = enricher._get("4D2387", None)
    assert second is first
    assert not calls


def test_memo_expiry_re_enters_get_uncached(tmp_path, monkeypatch):
    enricher = AdsbdbEnricher(cache_dir=tmp_path)

    calls = []
    def fake_get_uncached(hex_id, callsign):
        calls.append(1)
        return {"aircraft": {"manufacturer": "Boeing"}}
    monkeypatch.setattr(enricher, "_get_uncached", fake_get_uncached)

    base = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: base)
    enricher._get("4D2387", None)
    assert len(calls) == 1

    monkeypatch.setattr(time, "monotonic", lambda: base + _MEMO_TTL_SECONDS + 1)
    enricher._get("4D2387", None)
    assert len(calls) == 2


def test_failures_are_memoised_across_concurrent_lookups(tmp_path, monkeypatch):
    calls = []
    call_lock = threading.Lock()

    def fake_fetch(hex_id, callsign=None):
        with call_lock:
            calls.append(1)
        time.sleep(0.05)
        return None

    enricher = AdsbdbEnricher(cache_dir=tmp_path)
    monkeypatch.setattr(enricher, "_fetch", fake_fetch)

    results: list = [object(), object(), object()]

    def worker(i: int) -> None:
        results[i] = enricher._get("40097D", None)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(calls) == 1
    assert all(r is None for r in results)


def test_different_callsigns_are_distinct_keys(tmp_path, monkeypatch):
    calls = []
    def fake_get_uncached(hex_id, callsign):
        calls.append(callsign)
        return {"aircraft": {"manufacturer": "Boeing"}}

    enricher = AdsbdbEnricher(cache_dir=tmp_path)
    monkeypatch.setattr(enricher, "_get_uncached", fake_get_uncached)

    enricher._get("40097D", "EZY123")
    enricher._get("40097D", None)

    assert calls == ["EZY123", None]


def test_sweep_bounds_the_dicts(tmp_path, monkeypatch):
    enricher = AdsbdbEnricher(cache_dir=tmp_path)
    monkeypatch.setattr(enricher, "_get_uncached", lambda hex_id, callsign: {"aircraft": {}})

    enricher.process([_make_aircraft(callsign="RYR54NN")])
    assert enricher._memo
    assert enricher._key_locks

    base = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: base + _MEMO_TTL_SECONDS + 1)

    enricher.process([])   # process() sweeps first, even with nothing to enrich
    assert enricher._memo == {}
    assert enricher._key_locks == {}
