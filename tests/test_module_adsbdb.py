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
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from modules.adsbdb import AdsbdbEnricher
from schemas.aircraft import (
    Aircraft, AircraftLocation, AircraftMeta, AircraftRaw,
    AircraftRoute, AircraftVector, Airframe,
)


FIXTURES = Path(__file__).parent / "fixtures"

# The fixture file mirrors a real adsbdb HTTP response.
_FULL_API_RESPONSE = json.loads((FIXTURES / "4D2387 response.json").read_text())
# The plugin stores / applies only the inner "response" object.
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
# 1. Skip when callsign is UNKNOWN
# ---------------------------------------------------------------------------

def test_skip_when_callsign_unknown(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr("modules.adsbdb.requests.get", lambda *a, **kw: called.append(1))
    enricher = AdsbdbEnricher(cache_dir=tmp_path)
    a = _make_aircraft(callsign=None)
    enricher.process([a])
    assert not called
    assert a.airframe.manufacturer is None
    assert list(tmp_path.iterdir()) == []


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
