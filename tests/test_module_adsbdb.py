"""
tests/test_module_adsbdb.py

Tests for the adsbdb enrichment module.

No real network calls — requests.get is monkeypatched throughout.

The module makes two independent lookups: /v0/aircraft/<HEX> for the airframe
and /v0/callsign/<CS> for today's route. Most tests here mock both, because
the defect this module was fixed for was the two being coupled.

Covers:
  1.  Independent halves — an airframe miss still yields a route, and vice versa
  2.  Cache hit, fresh — no HTTP call
  3.  Cache miss → fetch → write both cache files + populate fields
  4.  Cache stale → fetch re-triggered
  5.  Stale + fetch fails → stale data applied
  6.  Definitive misses → not_found markers written, fields stay UNKNOWN
  7.  404 marker honoured — no HTTP call
  8.  UNKNOWN-only writes — pre-set fields not overwritten; raw.adsbdb overwritten
  9.  Rate limit honoured — no API call, fields UNKNOWN
  10. Callsign normalisation — trailing space trimmed, filename uppercased
  11. Shared instance — the factory pools by (name, cfg)
  12. One permit per fetch — guard lives in the request path, not the caller
  13. In-memory memo — concurrent stampede prevention, per key space
  14. Separate TTLs — the airframe cache outlives the route cache
  15. log_unresolved — one line per (hex, callsign), with the right reason
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from modules import clear_module_pool, get_module
from modules.adsbdb import (
    AdsbdbEnricher, _AIRCRAFT_TTL_SECONDS, _FLAG_LADD, _FLAG_PIA,
    _MEMO_TTL_SECONDS, _RATE_60S, _ROUTE_TTL_SECONDS,
)
from schemas.aircraft import (
    Aircraft, AircraftLocation, AircraftMeta, AircraftRaw,
    AircraftRoute, AircraftVector, Airframe,
)


FIXTURES = Path(__file__).parent / "fixtures"

# The fixture file mirrors a real adsbdb HTTP response.
_FULL_API_RESPONSE = json.loads((FIXTURES / "4D2387 response.json").read_text())
_API_INNER = _FULL_API_RESPONSE["response"]

# The two halves, as each endpoint serves them.
_AIRCRAFT_FRAGMENT = {"aircraft":    _API_INNER["aircraft"]}
_ROUTE_FRAGMENT    = {"flightroute": _API_INNER["flightroute"]}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_aircraft(callsign=None, operator=None, hex_id="4D2387", db_flags=None) -> Aircraft:
    return Aircraft(
        meta      = AircraftMeta(icao_hex=hex_id, ingestor="test", reception_type="mlat"),
        location  = AircraftLocation(),
        direction = AircraftVector(),
        route     = AircraftRoute(callsign=callsign),
        airframe  = Airframe(operator=operator, db_flags=db_flags),
        raw       = AircraftRaw(),
    )


class _Resp:
    """A stand-in for a requests Response.

    Deliberately not a MagicMock: _mock_endpoints treats a callable as a
    URL-dispatching factory, and a MagicMock is callable.
    """

    def __init__(self, status_code: int = 200, payload=None) -> None:
        self.status_code = status_code
        self._payload = payload

    def __bool__(self) -> bool:
        # Mirrors requests.Response.__bool__, which is `self.ok` — any 4xx/5xx
        # is falsy. Getting this wrong once hid 404s from the status check.
        return self.status_code < 400

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload


def _resp(status_code=200, payload=None) -> _Resp:
    return _Resp(status_code, payload)


def _mock_endpoints(monkeypatch, aircraft=None, route=None, calls=None):
    """Route each endpoint to its own canned response.

    `aircraft` / `route` are either a MagicMock response or a callable taking
    the URL. Anything not supplied answers 200 with the matching fixture half.
    """
    if aircraft is None:
        aircraft = _resp(200, {"response": _AIRCRAFT_FRAGMENT})
    if route is None:
        route = _resp(200, {"response": _ROUTE_FRAGMENT})

    def mock_get(url, **kw):
        if calls is not None:
            calls.append(url)
        chosen = aircraft if "/v0/aircraft/" in url else route
        return chosen(url) if callable(chosen) else chosen

    monkeypatch.setattr("modules.adsbdb.requests.get", mock_get)


def _mock_error(monkeypatch) -> None:
    def _raise(*a, **kw):
        raise ConnectionError("no network")
    monkeypatch.setattr("modules.adsbdb.requests.get", _raise)


def _write_cache(root: Path, kind: str, key: str, data: dict, age_seconds: float = 0.0) -> Path:
    directory = root / kind
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{key}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    if age_seconds:
        old = time.time() - age_seconds
        os.utime(path, (old, old))
    return path


# ===========================================================================
# 1. The two halves are independent — this is the defect the brief fixes
# ===========================================================================

def test_airframe_miss_still_yields_route(tmp_path, monkeypatch):
    # The BAW171 case: adsbdb does not know the hex, but knows the route.
    # Before the lookups were split, the unknown airframe discarded the route.
    _mock_endpoints(
        monkeypatch,
        aircraft = _resp(200, {"response": "unknown aircraft"}),
    )
    enricher = AdsbdbEnricher(cache_dir=tmp_path)
    a = _make_aircraft(callsign="BAW171")
    enricher.process([a])

    assert a.route.origin_iata         == "REU"
    assert a.route.destination_iata    == "LBA"
    assert a.route.airline_name        == "Ryanair"
    assert a.airframe.registration     is None   # airframe genuinely unknown


def test_route_miss_still_yields_airframe(tmp_path, monkeypatch):
    # The mirror case: SHT18A-style gap in adsbdb's route data.
    _mock_endpoints(monkeypatch, route=_resp(404))
    enricher = AdsbdbEnricher(cache_dir=tmp_path)
    a = _make_aircraft(callsign="SHT18A")
    enricher.process([a])

    assert a.airframe.registration  == "9H-VUZ"
    assert a.airframe.manufacturer  == "Boeing"
    assert a.route.origin_iata      is None


def test_both_miss_leaves_fields_unknown_and_writes_one_marker_per_space(tmp_path, monkeypatch):
    _mock_endpoints(
        monkeypatch,
        aircraft = _resp(200, {"response": "unknown aircraft"}),
        route    = _resp(404),
    )
    enricher = AdsbdbEnricher(cache_dir=tmp_path)
    a = _make_aircraft(callsign="NOPE123")
    enricher.process([a])

    assert a.airframe.manufacturer is None
    assert a.route.origin_iata     is None
    assert a.raw.adsbdb            == {}

    for kind, key in ((("aircraft"), "4D2387"), ("route", "NOPE123")):
        marker = json.loads((tmp_path / kind / f"{key}.json").read_text())
        assert marker.get("not_found") is True
        assert "checked_at" in marker


def test_no_callsign_attempts_no_route_call(tmp_path, monkeypatch):
    calls: list[str] = []
    _mock_endpoints(monkeypatch, calls=calls)
    enricher = AdsbdbEnricher(cache_dir=tmp_path)
    a = _make_aircraft(callsign=None)
    enricher.process([a])

    assert len(calls) == 1
    assert "/v0/aircraft/4D2387" in calls[0]
    assert not any("/v0/callsign/" in u for u in calls)

    assert a.airframe.manufacturer == "Boeing"
    assert a.airframe.registration == "9H-VUZ"
    assert (tmp_path / "aircraft" / "4D2387.json").exists()


def test_endpoints_are_queried_without_a_callsign_query_param(tmp_path, monkeypatch):
    calls: list[str] = []
    _mock_endpoints(monkeypatch, calls=calls)
    AdsbdbEnricher(cache_dir=tmp_path).process([_make_aircraft(callsign="RYR54NN")])

    assert sorted(calls) == [
        "https://api.adsbdb.com/v0/aircraft/4D2387",
        "https://api.adsbdb.com/v0/callsign/RYR54NN",
    ]


# ===========================================================================
# 2. Cache hit, fresh — no HTTP call
# ===========================================================================

def test_cache_hit_fresh_no_http_call(tmp_path, monkeypatch):
    _write_cache(tmp_path, "aircraft", "4D2387", _AIRCRAFT_FRAGMENT)
    _write_cache(tmp_path, "route", "RYR54NN", _ROUTE_FRAGMENT)

    called = []
    monkeypatch.setattr("modules.adsbdb.requests.get", lambda *a, **kw: called.append(1))
    enricher = AdsbdbEnricher(cache_dir=tmp_path)
    a = _make_aircraft(callsign="RYR54NN")
    enricher.process([a])

    assert not called
    assert a.airframe.registration == "9H-VUZ"
    assert a.route.origin_iata     == "REU"


# ===========================================================================
# 3. Cache miss → fetch → write cache files + populate fields
# ===========================================================================

def test_cache_miss_fetch_writes_caches_and_populates_fields(tmp_path, monkeypatch):
    _mock_endpoints(monkeypatch)
    enricher = AdsbdbEnricher(cache_dir=tmp_path)
    a = _make_aircraft(callsign="RYR54NN")
    enricher.process([a])

    assert (tmp_path / "aircraft" / "4D2387.json").exists()
    assert (tmp_path / "route" / "RYR54NN.json").exists()

    assert a.airframe.manufacturer     == "Boeing"
    assert a.airframe.registration     == "9H-VUZ"
    assert a.airframe.type_description == "737MAX 8 200"
    assert a.airframe.operator         == "Malta Air"
    assert a.route.origin_iata         == "REU"
    assert a.route.origin_name         == "Reus Airport"
    assert a.route.origin_country      == "Spain"
    assert a.route.destination_iata    == "LBA"
    assert a.route.destination_name    == "Leeds Bradford Airport"
    assert a.route.destination_country == "United Kingdom"
    assert a.route.airline_name        == "Ryanair"
    assert a.route.airline_country     == "Ireland"

    # raw.adsbdb holds the merged dict, so a consumer sees both halves.
    assert a.raw.adsbdb == _API_INNER


def test_apply_populates_municipalities(tmp_path, monkeypatch):
    # The cached route files already hold "municipality" — the field was in the
    # API response all along, simply not mapped — so existing caches fill this
    # in on their next read without a flush.
    _mock_endpoints(monkeypatch)
    a = _make_aircraft(callsign="RYR54NN")
    AdsbdbEnricher(cache_dir=tmp_path).process([a])

    assert a.route.origin_municipality      == "Reus"
    assert a.route.destination_municipality == "Leeds"
    # The airport's full name is kept, not repurposed.
    assert a.route.origin_name              == "Reus Airport"
    assert a.route.destination_name         == "Leeds Bradford Airport"


def test_enriched_municipalities_survive_storage(tmp_path, monkeypatch):
    from storage.disk_drive import DiskDriveStorage

    _mock_endpoints(monkeypatch)
    a = _make_aircraft(callsign="RYR54NN")
    a.meta.observed_at = datetime.now(timezone.utc)
    a.location.seen_seconds = 0.0
    AdsbdbEnricher(cache_dir=tmp_path / "cache").process([a])

    backend = DiskDriveStorage(tmp_path / "store")
    backend.save_aircraft_array([a])
    restored = backend.retrieve_aircraft_objects()[0]

    assert restored.route.origin_municipality      == "Reus"
    assert restored.route.destination_municipality == "Leeds"


def test_cache_files_hold_only_their_own_half(tmp_path, monkeypatch):
    # The two spaces held the same merged payload before the split; they hold
    # genuinely different content now and must not collide.
    _mock_endpoints(monkeypatch)
    AdsbdbEnricher(cache_dir=tmp_path).process([_make_aircraft(callsign="RYR54NN")])

    airframe = json.loads((tmp_path / "aircraft" / "4D2387.json").read_text())
    route    = json.loads((tmp_path / "route" / "RYR54NN.json").read_text())

    assert set(airframe) == {"aircraft"}
    assert set(route)    == {"flightroute"}


# ===========================================================================
# 4. Cache stale → fetch re-triggered
# ===========================================================================

def test_cache_stale_triggers_fetch(tmp_path, monkeypatch):
    _write_cache(tmp_path, "aircraft", "4D2387", _AIRCRAFT_FRAGMENT,
                 age_seconds=_AIRCRAFT_TTL_SECONDS + 60)
    _write_cache(tmp_path, "route", "RYR54NN", _ROUTE_FRAGMENT,
                 age_seconds=_ROUTE_TTL_SECONDS + 60)

    calls: list[str] = []
    _mock_endpoints(monkeypatch, calls=calls)
    AdsbdbEnricher(cache_dir=tmp_path).process([_make_aircraft(callsign="RYR54NN")])

    assert len(calls) == 2


# ===========================================================================
# 5. Stale + fetch fails → stale data applied
# ===========================================================================

def test_stale_fetch_fails_uses_stale_data(tmp_path, monkeypatch):
    _write_cache(tmp_path, "aircraft", "4D2387", _AIRCRAFT_FRAGMENT,
                 age_seconds=_AIRCRAFT_TTL_SECONDS + 60)
    _write_cache(tmp_path, "route", "RYR54NN", _ROUTE_FRAGMENT,
                 age_seconds=_ROUTE_TTL_SECONDS + 60)

    _mock_error(monkeypatch)
    enricher = AdsbdbEnricher(cache_dir=tmp_path)
    a = _make_aircraft(callsign="RYR54NN")
    enricher.process([a])

    assert a.airframe.manufacturer == "Boeing"
    assert a.route.origin_iata     == "REU"
    assert a.raw.adsbdb            == _API_INNER


# ===========================================================================
# 6. Definitive misses are cached; transient failures are not
# ===========================================================================

def test_404_writes_not_found_marker_and_fields_stay_unknown(tmp_path, monkeypatch):
    _mock_endpoints(monkeypatch, aircraft=_resp(404), route=_resp(404))
    enricher = AdsbdbEnricher(cache_dir=tmp_path)
    a = _make_aircraft(callsign="RYR54NN")
    enricher.process([a])

    marker = json.loads((tmp_path / "route" / "RYR54NN.json").read_text())
    assert marker.get("not_found") is True
    assert "checked_at" in marker

    assert a.airframe.manufacturer is None
    assert a.route.origin_iata     is None
    assert a.raw.adsbdb            == {}


def test_falsy_404_response_still_caches_a_marker(tmp_path, monkeypatch):
    # requests.Response is falsy for any error status, so a truthiness guard in
    # the request path would drop the 404 into the "transient" bucket and never
    # write a marker. Live 4080C0 returns exactly this.
    resp = _resp(404, {"response": "unknown aircraft"})
    assert not resp, "test double must be falsy like a real error Response"

    _mock_endpoints(monkeypatch, aircraft=resp, route=resp)
    enricher = AdsbdbEnricher(cache_dir=tmp_path)
    enricher.process([_make_aircraft(callsign="BAW171")])

    marker = json.loads((tmp_path / "aircraft" / "4D2387.json").read_text())
    assert marker.get("not_found") is True

    # And the marker means the next cycle costs no call at all.
    calls: list[str] = []
    monkeypatch.setattr("modules.adsbdb.requests.get",
                        lambda url, **kw: calls.append(url) or _resp(404))
    AdsbdbEnricher(cache_dir=tmp_path).process([_make_aircraft(callsign="BAW171")])
    assert calls == []


def test_unknown_string_response_is_a_cacheable_miss(tmp_path, monkeypatch):
    # The aircraft endpoint signals an unknown hex with a 200 and a string,
    # not a 404. It is still definitive, so it is still cacheable.
    _mock_endpoints(monkeypatch, aircraft=_resp(200, {"response": "unknown aircraft"}))
    AdsbdbEnricher(cache_dir=tmp_path).process([_make_aircraft(callsign="RYR54NN")])

    marker = json.loads((tmp_path / "aircraft" / "4D2387.json").read_text())
    assert marker.get("not_found") is True


@pytest.mark.parametrize("failure", ["timeout", "500", "429"])
def test_transient_failure_does_not_write_not_found_marker(tmp_path, monkeypatch, failure):
    # A timeout or a 500 must never be recorded as "no such aircraft".
    if failure == "timeout":
        _mock_error(monkeypatch)
    else:
        code = int(failure)
        _mock_endpoints(monkeypatch, aircraft=_resp(code), route=_resp(code))

    enricher = AdsbdbEnricher(cache_dir=tmp_path)
    enricher.process([_make_aircraft(callsign="RYR54NN")])

    assert not (tmp_path / "aircraft" / "4D2387.json").exists()
    assert not (tmp_path / "route" / "RYR54NN.json").exists()


# ===========================================================================
# 7. Fresh not-found marker honoured — no HTTP call
# ===========================================================================

def test_fresh_not_found_marker_prevents_http_call(tmp_path, monkeypatch):
    marker = {"not_found": True, "checked_at": "2026-05-13T12:00:00+00:00"}
    _write_cache(tmp_path, "aircraft", "4D2387", marker)
    _write_cache(tmp_path, "route", "RYR54NN", marker)

    called = []
    monkeypatch.setattr("modules.adsbdb.requests.get", lambda *a, **kw: called.append(1))
    AdsbdbEnricher(cache_dir=tmp_path).process([_make_aircraft(callsign="RYR54NN")])
    assert not called


def test_not_found_in_one_space_does_not_suppress_the_other(tmp_path, monkeypatch):
    # A cached airframe miss must not stop the route being fetched.
    _write_cache(tmp_path, "aircraft", "4D2387",
                 {"not_found": True, "checked_at": "2026-05-13T12:00:00+00:00"})

    calls: list[str] = []
    _mock_endpoints(monkeypatch, calls=calls)
    a = _make_aircraft(callsign="RYR54NN")
    AdsbdbEnricher(cache_dir=tmp_path).process([a])

    assert calls == ["https://api.adsbdb.com/v0/callsign/RYR54NN"]
    assert a.route.origin_iata == "REU"


# ===========================================================================
# 8. UNKNOWN-only writes
# ===========================================================================

def test_adsbdb_writes_description_and_never_the_type_code(tmp_path, monkeypatch):
    # adsbdb's string is the better display value and is preferred over
    # whatever is already there — but it must not touch the designator.
    _mock_endpoints(monkeypatch)
    a = _make_aircraft(callsign="RYR54NN")
    a.airframe.type_code        = "B38M"
    a.airframe.type_description = "BOEING 737 MAX 8"

    AdsbdbEnricher(cache_dir=tmp_path).process([a])

    assert a.airframe.type_description == "737MAX 8 200"   # overwritten, deliberately
    assert a.airframe.type_code        == "B38M"           # untouched


def test_adsbdb_does_not_invent_a_type_code(tmp_path, monkeypatch):
    _mock_endpoints(monkeypatch)
    a = _make_aircraft(callsign="RYR54NN")
    AdsbdbEnricher(cache_dir=tmp_path).process([a])

    assert a.airframe.type_description == "737MAX 8 200"
    assert a.airframe.type_code        is None


def test_unknown_only_does_not_overwrite_preset_operator(tmp_path, monkeypatch):
    _mock_endpoints(monkeypatch)
    enricher = AdsbdbEnricher(cache_dir=tmp_path)
    a = _make_aircraft(callsign="RYR54NN", operator="PreSet")
    enricher.process([a])

    assert a.airframe.operator == "PreSet"    # not overwritten
    assert a.raw.adsbdb        == _API_INNER  # always overwritten


# ===========================================================================
# 9. Rate limit honoured — no API call, fields UNKNOWN
# ===========================================================================

def test_rate_limit_skips_api_call(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr("modules.adsbdb.requests.get", lambda *a, **kw: called.append(1))
    enricher = AdsbdbEnricher(cache_dir=tmp_path)

    now = time.monotonic()
    for _ in range(_RATE_60S):
        enricher._call_times.append(now - 1)   # all within last 60 s

    a = _make_aircraft(callsign="RYR54NN")
    enricher.process([a])

    assert not called
    assert a.airframe.manufacturer is None


def test_rate_limited_lookup_is_not_cached_as_a_miss(tmp_path, monkeypatch):
    monkeypatch.setattr("modules.adsbdb.requests.get", lambda *a, **kw: None)
    enricher = AdsbdbEnricher(cache_dir=tmp_path)
    now = time.monotonic()
    for _ in range(_RATE_60S):
        enricher._call_times.append(now - 1)

    enricher.process([_make_aircraft(callsign="RYR54NN")])
    assert not (tmp_path / "aircraft").exists() or \
           not (tmp_path / "aircraft" / "4D2387.json").exists()


# ===========================================================================
# 10. Callsign normalisation — trailing space trimmed, filename uppercased
# ===========================================================================

def test_callsign_normalised_to_trimmed_uppercase_filename(tmp_path, monkeypatch):
    _mock_endpoints(monkeypatch)
    AdsbdbEnricher(cache_dir=tmp_path).process([_make_aircraft(callsign="ryr54nn ")])

    assert     (tmp_path / "route" / "RYR54NN.json").exists()
    assert not (tmp_path / "route" / "RYR54NN .json").exists()


# ===========================================================================
# 11. Shared instance — the factory pools by (name, cfg), not the module
# ===========================================================================

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


def test_log_unresolved_defaults_to_false(reset_module_pool):
    assert get_module("adsbdb", {})._log_unresolved is False


def test_log_unresolved_read_from_config(reset_module_pool):
    assert get_module("adsbdb", {"log_unresolved": True})._log_unresolved is True


# ===========================================================================
# 12. One permit per fetch — guard lives in the request path, not the caller
# ===========================================================================

def test_fetch_records_one_permit_per_call(tmp_path, monkeypatch):
    _mock_error(monkeypatch)
    enricher = AdsbdbEnricher(cache_dir=tmp_path)
    enricher._fetch_aircraft("4D2387")
    enricher._fetch_route("RYR54NN")
    assert len(enricher._call_times) == 2


def test_fetch_denied_when_rate_limited_and_no_http(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr("modules.adsbdb.requests.get", lambda *a, **kw: called.append(1))
    enricher = AdsbdbEnricher(cache_dir=tmp_path)
    now = time.monotonic()
    for _ in range(_RATE_60S):
        enricher._call_times.append(now - 1)

    assert enricher._fetch_aircraft("4D2387") is None
    assert enricher._fetch_route("RYR54NN") is None
    assert not called


# ===========================================================================
# 13. In-memory memo — concurrent stampede prevention, per key space
# ===========================================================================

def test_concurrent_lookups_produce_one_call_of_each_kind(tmp_path, monkeypatch):
    # Adapted from the original stampede test: three chains cold on the same
    # aircraft must still produce exactly one aircraft call and one route call.
    calls: list[str] = []
    call_lock = threading.Lock()

    def mock_get(url, **kw):
        with call_lock:
            calls.append(url)
        time.sleep(0.05)
        payload = _AIRCRAFT_FRAGMENT if "/v0/aircraft/" in url else _ROUTE_FRAGMENT
        return _resp(200, {"response": payload})

    monkeypatch.setattr("modules.adsbdb.requests.get", mock_get)
    enricher = AdsbdbEnricher(cache_dir=tmp_path)

    aircraft = [_make_aircraft(callsign="RYR54NN") for _ in range(3)]

    threads = [threading.Thread(target=enricher.process, args=([a],)) for a in aircraft]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(1 for u in calls if "/v0/aircraft/" in u) == 1
    assert sum(1 for u in calls if "/v0/callsign/" in u) == 1
    for a in aircraft:
        assert a.airframe.registration == "9H-VUZ"
        assert a.route.origin_iata     == "REU"


def test_memo_hit_avoids_disk(tmp_path, monkeypatch):
    _mock_endpoints(monkeypatch)
    enricher = AdsbdbEnricher(cache_dir=tmp_path)

    first = enricher._get("aircraft", "4D2387")
    assert first is not None
    cache_file = tmp_path / "aircraft" / "4D2387.json"
    assert cache_file.exists()
    cache_file.unlink()

    calls = []
    monkeypatch.setattr(enricher, "_fetch_aircraft", lambda *a, **kw: calls.append(1))

    second = enricher._get("aircraft", "4D2387")
    assert second is first
    assert not calls


def test_memo_expiry_re_enters_get_uncached(tmp_path, monkeypatch):
    enricher = AdsbdbEnricher(cache_dir=tmp_path)

    calls = []
    def fake_get_uncached(kind, key):
        calls.append(1)
        return {"aircraft": {"manufacturer": "Boeing"}}
    monkeypatch.setattr(enricher, "_get_uncached", fake_get_uncached)

    base = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: base)
    enricher._get("aircraft", "4D2387")
    assert len(calls) == 1

    monkeypatch.setattr(time, "monotonic", lambda: base + _MEMO_TTL_SECONDS + 1)
    enricher._get("aircraft", "4D2387")
    assert len(calls) == 2


def test_failures_are_memoised_across_concurrent_lookups(tmp_path, monkeypatch):
    calls = []
    call_lock = threading.Lock()

    def fake_fetch(key):
        with call_lock:
            calls.append(1)
        time.sleep(0.05)
        return None

    enricher = AdsbdbEnricher(cache_dir=tmp_path)
    monkeypatch.setattr(enricher, "_fetch_aircraft", fake_fetch)

    results: list = [object(), object(), object()]

    def worker(i: int) -> None:
        results[i] = enricher._get("aircraft", "40097D")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(calls) == 1
    assert all(r is None for r in results)


def test_key_spaces_are_distinct(tmp_path, monkeypatch):
    # A hex and a callsign that happen to share a string must not collide.
    calls = []
    def fake_get_uncached(kind, key):
        calls.append((kind, key))
        return {}

    enricher = AdsbdbEnricher(cache_dir=tmp_path)
    monkeypatch.setattr(enricher, "_get_uncached", fake_get_uncached)

    enricher._get("aircraft", "SAME")
    enricher._get("route", "SAME")

    assert calls == [("aircraft", "SAME"), ("route", "SAME")]


def test_different_callsigns_are_distinct_keys(tmp_path, monkeypatch):
    calls = []
    def fake_get_uncached(kind, key):
        calls.append(key)
        return {"flightroute": {}}

    enricher = AdsbdbEnricher(cache_dir=tmp_path)
    monkeypatch.setattr(enricher, "_get_uncached", fake_get_uncached)

    enricher._get("route", "EZY123")
    enricher._get("route", "RYR54NN")

    assert calls == ["EZY123", "RYR54NN"]


def test_sweep_bounds_the_dicts(tmp_path, monkeypatch):
    enricher = AdsbdbEnricher(cache_dir=tmp_path)
    monkeypatch.setattr(enricher, "_get_uncached", lambda kind, key: {"aircraft": {}})

    enricher.process([_make_aircraft(callsign="RYR54NN")])
    assert enricher._memo
    assert enricher._key_locks

    base = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: base + _MEMO_TTL_SECONDS + 1)

    enricher.process([])   # process() sweeps first, even with nothing to enrich
    assert enricher._memo == {}
    assert enricher._key_locks == {}


# ===========================================================================
# 14. Separate TTLs — the airframe cache genuinely outlives the route cache
# ===========================================================================

def test_airframe_cache_survives_past_route_ttl_without_refetch(tmp_path, monkeypatch):
    # Age both caches to just past the route TTL. The route must be refetched;
    # the airframe, on a 7-day TTL, must not be.
    age = _ROUTE_TTL_SECONDS + 60
    _write_cache(tmp_path, "aircraft", "4D2387", _AIRCRAFT_FRAGMENT, age_seconds=age)
    _write_cache(tmp_path, "route", "RYR54NN", _ROUTE_FRAGMENT, age_seconds=age)

    calls: list[str] = []
    _mock_endpoints(monkeypatch, calls=calls)
    a = _make_aircraft(callsign="RYR54NN")
    AdsbdbEnricher(cache_dir=tmp_path).process([a])

    assert calls == ["https://api.adsbdb.com/v0/callsign/RYR54NN"]
    assert a.airframe.registration == "9H-VUZ"   # still applied, from cache


def test_airframe_cache_does_expire_past_its_own_ttl(tmp_path, monkeypatch):
    _write_cache(tmp_path, "aircraft", "4D2387", _AIRCRAFT_FRAGMENT,
                 age_seconds=_AIRCRAFT_TTL_SECONDS + 60)

    calls: list[str] = []
    _mock_endpoints(monkeypatch, calls=calls)
    AdsbdbEnricher(cache_dir=tmp_path).process([_make_aircraft(callsign=None)])

    assert calls == ["https://api.adsbdb.com/v0/aircraft/4D2387"]


# ===========================================================================
# 15. log_unresolved
# ===========================================================================

def _unresolved_lines(tmp_path: Path) -> list[dict]:
    path = tmp_path / "route" / "unresolved.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def test_log_unresolved_defaults_off_and_writes_nothing(tmp_path, monkeypatch):
    _mock_endpoints(monkeypatch, route=_resp(404))
    AdsbdbEnricher(cache_dir=tmp_path).process([_make_aircraft(callsign="SHT18A")])
    assert not (tmp_path / "route" / "unresolved.jsonl").exists()


def test_log_unresolved_records_unknown_callsign(tmp_path, monkeypatch):
    _mock_endpoints(monkeypatch, route=_resp(404))
    enricher = AdsbdbEnricher(cache_dir=tmp_path, log_unresolved=True)
    enricher.process([_make_aircraft(callsign="SHT18A")])

    lines = _unresolved_lines(tmp_path)
    assert len(lines) == 1
    entry = lines[0]
    assert entry["hex"]          == "4D2387"
    assert entry["callsign"]     == "SHT18A"
    assert entry["reason"]       == "unknown_callsign"
    assert entry["registration"] == "9H-VUZ"   # airframe half still resolved
    assert entry["at"]


def test_log_unresolved_records_no_callsign(tmp_path, monkeypatch):
    _mock_endpoints(monkeypatch)
    enricher = AdsbdbEnricher(cache_dir=tmp_path, log_unresolved=True)
    enricher.process([_make_aircraft(callsign=None)])

    lines = _unresolved_lines(tmp_path)
    assert len(lines) == 1
    assert lines[0]["reason"]   == "no_callsign"
    assert lines[0]["callsign"] is None


def test_log_unresolved_records_fetch_failed(tmp_path, monkeypatch):
    # A transient failure is a different finding from a data gap: it has a
    # different fix, so it gets a different reason.
    _mock_endpoints(monkeypatch, route=_resp(500))
    enricher = AdsbdbEnricher(cache_dir=tmp_path, log_unresolved=True)
    enricher.process([_make_aircraft(callsign="VJT630")])

    lines = _unresolved_lines(tmp_path)
    assert len(lines) == 1
    assert lines[0]["reason"] == "fetch_failed"


def test_log_unresolved_deduplicates_on_hex_and_callsign(tmp_path, monkeypatch):
    _mock_endpoints(monkeypatch, route=_resp(404))
    enricher = AdsbdbEnricher(cache_dir=tmp_path, log_unresolved=True)

    for _ in range(5):
        enricher.process([_make_aircraft(callsign="SHT18A")])

    assert len(_unresolved_lines(tmp_path)) == 1


def test_log_unresolved_records_each_distinct_pair(tmp_path, monkeypatch):
    _mock_endpoints(monkeypatch, route=_resp(404))
    enricher = AdsbdbEnricher(cache_dir=tmp_path, log_unresolved=True)

    enricher.process([_make_aircraft(callsign="SHT18A", hex_id="4D2387")])
    enricher.process([_make_aircraft(callsign="VJT630", hex_id="ABCDEF")])

    lines = _unresolved_lines(tmp_path)
    assert {l["callsign"] for l in lines} == {"SHT18A", "VJT630"}


def test_log_unresolved_writes_nothing_when_route_resolves(tmp_path, monkeypatch):
    _mock_endpoints(monkeypatch)
    enricher = AdsbdbEnricher(cache_dir=tmp_path, log_unresolved=True)
    enricher.process([_make_aircraft(callsign="RYR54NN")])

    assert _unresolved_lines(tmp_path) == []


def test_log_unresolved_dedups_across_memo_expiry(tmp_path, monkeypatch):
    """One line per (hex, callsign) for the life of the process.

    The dedup set is consulted where the line is written, not inside the
    memo-guarded lookup, so a memo expiry that re-attempts the route and fails
    again must not produce a second line. The lookup genuinely re-runs here —
    both the memo and the on-disk marker are expired between cycles.
    """
    _mock_endpoints(monkeypatch, aircraft=_resp(404), route=_resp(404))
    enricher = AdsbdbEnricher(cache_dir=tmp_path, log_unresolved=True)

    for _ in range(4):
        enricher._memo.clear()
        enricher._key_locks.clear()
        for cached in tmp_path.rglob("*.json"):
            old = time.time() - (_ROUTE_TTL_SECONDS + 120)
            os.utime(cached, (old, old))
        enricher.process([_make_aircraft(callsign="SHT18A")])

    assert len(_unresolved_lines(tmp_path)) == 1


def test_log_unresolved_dedups_when_only_the_memo_expires(tmp_path, monkeypatch):
    # The clock-advance form: memo TTL passes between cycles.
    _mock_endpoints(monkeypatch, route=_resp(404))
    enricher = AdsbdbEnricher(cache_dir=tmp_path, log_unresolved=True)

    base = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: base)
    enricher.process([_make_aircraft(callsign="SHT18A")])

    monkeypatch.setattr(time, "monotonic", lambda: base + _MEMO_TTL_SECONDS + 5)
    enricher.process([_make_aircraft(callsign="SHT18A")])

    monkeypatch.setattr(time, "monotonic", lambda: base + 4 * 60)
    enricher.process([_make_aircraft(callsign="SHT18A")])

    assert len(_unresolved_lines(tmp_path)) == 1


def test_log_unresolved_still_separates_distinct_callsigns_for_one_hex(tmp_path, monkeypatch):
    # Dedup is on the pair, not the hex: a hex that changes callsign is a
    # genuinely different lookup and gets its own line.
    _mock_endpoints(monkeypatch, route=_resp(404))
    enricher = AdsbdbEnricher(cache_dir=tmp_path, log_unresolved=True)

    enricher.process([_make_aircraft(callsign="SHT18A", hex_id="4CA123")])
    enricher.process([_make_aircraft(callsign="SHT19B", hex_id="4CA123")])

    lines = _unresolved_lines(tmp_path)
    assert len(lines) == 2
    assert {l["callsign"] for l in lines} == {"SHT18A", "SHT19B"}
    assert {l["hex"] for l in lines} == {"4CA123"}


# ===========================================================================
# 16. Non-ICAO addresses — readsb's '~' prefix
# ===========================================================================

def test_non_icao_address_triggers_no_http_call(tmp_path, monkeypatch):
    # A '~' address is a TIS-B relay or anonymised target. There is no airframe
    # behind it, so both lookups are skipped.
    calls: list[str] = []
    _mock_endpoints(monkeypatch, calls=calls)
    enricher = AdsbdbEnricher(cache_dir=tmp_path)
    enricher.process([_make_aircraft(hex_id="~085ED5", callsign="ABC123")])

    assert calls == []


def test_non_icao_address_writes_no_cache_file(tmp_path, monkeypatch):
    # A 404 would otherwise cache a marker keyed on something that is not an
    # aircraft identifier, so the cache accumulates entries for things that
    # cannot exist.
    _mock_endpoints(monkeypatch, aircraft=_resp(404), route=_resp(404))
    enricher = AdsbdbEnricher(cache_dir=tmp_path)
    enricher.process([_make_aircraft(hex_id="~DC078E", callsign="ABC123")])

    assert list(tmp_path.rglob("*.json")) == []


def test_non_icao_address_writes_no_unresolved_line(tmp_path, monkeypatch):
    # The log records gaps in adsbdb's data. A non-ICAO address was never a
    # candidate, so logging it would record a decision Squawk made instead.
    _mock_endpoints(monkeypatch, route=_resp(404))
    enricher = AdsbdbEnricher(cache_dir=tmp_path, log_unresolved=True)
    enricher.process([_make_aircraft(hex_id="~085ED5", callsign="ABC123")])
    enricher.process([_make_aircraft(hex_id="~085ED5", callsign=None)])

    assert _unresolved_lines(tmp_path) == []


def test_non_icao_aircraft_passes_through_unchanged(tmp_path, monkeypatch):
    # They are real contacts, just unidentifiable ones: still displayed, simply
    # carrying no enrichment. Never dropped from the list.
    _mock_endpoints(monkeypatch)
    enricher = AdsbdbEnricher(cache_dir=tmp_path)

    tilde  = _make_aircraft(hex_id="~085ED5", callsign="ABC123")
    normal = _make_aircraft(callsign="RYR54NN")
    result = enricher.process([tilde, normal])

    assert len(result) == 2
    assert result[0] is tilde
    assert tilde.airframe.registration is None
    assert tilde.raw.adsbdb == {}
    # The real aircraft alongside it is still enriched.
    assert normal.airframe.registration == "9H-VUZ"
    assert normal.route.origin_iata     == "REU"


# ===========================================================================
# 17. Privacy flags — PIA and LADD suppress different lookups
# ===========================================================================

_FLAG_MILITARY    = 1
_FLAG_INTERESTING = 2


def _kinds(calls: list[str]) -> set[str]:
    return {"aircraft" if "/v0/aircraft/" in u else "route" for u in calls}


def test_pia_skips_both_lookups(tmp_path, monkeypatch):
    # A Privacy ICAO Address is temporary and identifies no airframe, so
    # neither lookup can succeed. Treated exactly like a '~' address.
    calls: list[str] = []
    _mock_endpoints(monkeypatch, calls=calls)
    enricher = AdsbdbEnricher(cache_dir=tmp_path, log_unresolved=True)
    a = _make_aircraft(callsign="RYR54NN", db_flags=_FLAG_PIA)
    enricher.process([a])

    assert calls == []
    assert list(tmp_path.rglob("*.json")) == []
    assert _unresolved_lines(tmp_path) == []
    assert a.airframe.registration is None


def test_ladd_skips_the_route_but_keeps_the_airframe(tmp_path, monkeypatch):
    """The asymmetry is the whole point of the change.

    LADD suppresses flight data, not the airframe record. Skipping both would
    throw away registration, type and operator that adsbdb gives for free.
    If this ever gets 'simplified' into skipping both calls, this test is what
    should stop it.
    """
    calls: list[str] = []
    _mock_endpoints(monkeypatch, calls=calls)
    enricher = AdsbdbEnricher(cache_dir=tmp_path, log_unresolved=True)
    a = _make_aircraft(callsign="RYR54NN", db_flags=_FLAG_LADD)
    enricher.process([a])

    assert _kinds(calls) == {"aircraft"}, f"route must not be queried: {calls}"

    # The airframe half still landed.
    assert a.airframe.registration == "9H-VUZ"
    assert a.airframe.manufacturer == "Boeing"
    # The route half did not.
    assert a.route.origin_iata  is None
    assert a.route.airline_name is None


def test_ladd_writes_one_suppressed_log_line(tmp_path, monkeypatch):
    # A route that plausibly exists and is deliberately withheld — worth
    # counting, unlike PIA and '~' which were never candidates.
    _mock_endpoints(monkeypatch)
    enricher = AdsbdbEnricher(cache_dir=tmp_path, log_unresolved=True)
    enricher.process([_make_aircraft(callsign="N12345", db_flags=_FLAG_LADD)])

    lines = _unresolved_lines(tmp_path)
    assert len(lines) == 1
    assert lines[0]["reason"]   == "suppressed"
    assert lines[0]["callsign"] == "N12345"


def test_ladd_route_cache_is_never_written(tmp_path, monkeypatch):
    _mock_endpoints(monkeypatch)
    AdsbdbEnricher(cache_dir=tmp_path).process([
        _make_aircraft(callsign="N12345", db_flags=_FLAG_LADD)
    ])
    assert not (tmp_path / "route" / "N12345.json").exists()
    assert (tmp_path / "aircraft" / "4D2387.json").exists()


def test_ladd_and_pia_together_means_pia_wins(tmp_path, monkeypatch):
    calls: list[str] = []
    _mock_endpoints(monkeypatch, calls=calls)
    enricher = AdsbdbEnricher(cache_dir=tmp_path, log_unresolved=True)
    enricher.process([
        _make_aircraft(callsign="RYR54NN", db_flags=_FLAG_PIA | _FLAG_LADD)
    ])

    assert calls == []
    assert _unresolved_lines(tmp_path) == []


@pytest.mark.parametrize("flags,label", [
    (_FLAG_MILITARY,                    "military"),
    (_FLAG_INTERESTING,                 "interesting"),
    (_FLAG_MILITARY | _FLAG_INTERESTING, "military+interesting"),
])
def test_descriptive_flags_suppress_nothing(tmp_path, monkeypatch, flags, label):
    # Military and interesting are descriptive, not privacy flags, and a
    # military aircraft's airframe is often in adsbdb.
    calls: list[str] = []
    _mock_endpoints(monkeypatch, calls=calls)
    a = _make_aircraft(callsign="RYR54NN", db_flags=flags)
    AdsbdbEnricher(cache_dir=tmp_path).process([a])

    assert _kinds(calls) == {"aircraft", "route"}, f"{label} must suppress nothing"
    assert a.route.origin_iata     == "REU"
    assert a.airframe.registration == "9H-VUZ"


def test_db_flags_none_suppresses_nothing(tmp_path, monkeypatch):
    # Absence of information is not information. A receiver without --db-file
    # reports no flags at all, and that must not read as "suppress".
    calls: list[str] = []
    _mock_endpoints(monkeypatch, calls=calls)
    a = _make_aircraft(callsign="RYR54NN", db_flags=None)
    AdsbdbEnricher(cache_dir=tmp_path).process([a])

    assert _kinds(calls) == {"aircraft", "route"}
    assert a.route.origin_iata == "REU"


def test_db_flags_zero_suppresses_nothing(tmp_path, monkeypatch):
    # 0 is "no flags set" — a positive statement, and equally permissive.
    calls: list[str] = []
    _mock_endpoints(monkeypatch, calls=calls)
    a = _make_aircraft(callsign="RYR54NN", db_flags=0)
    AdsbdbEnricher(cache_dir=tmp_path).process([a])

    assert _kinds(calls) == {"aircraft", "route"}
    assert a.route.origin_iata == "REU"


def test_flagged_aircraft_pass_through_process_unchanged(tmp_path, monkeypatch):
    _mock_endpoints(monkeypatch)
    enricher = AdsbdbEnricher(cache_dir=tmp_path)
    pia  = _make_aircraft(hex_id="A00001", callsign="N1", db_flags=_FLAG_PIA)
    ladd = _make_aircraft(hex_id="A00002", callsign="N2", db_flags=_FLAG_LADD)
    normal = _make_aircraft(callsign="RYR54NN")

    result = enricher.process([pia, ladd, normal])
    assert result == [pia, ladd, normal]
