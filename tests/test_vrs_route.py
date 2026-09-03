"""
tests/test_vrs_route.py

Tests for the vrs_route enrichment module.

No real network or zip download here: a small SQLite file is built directly
with the four tables vrs_route reads (routes, airports, countries,
airlines), wrapped in a FakeSource standing in for VrsStandingData.

Covers:
  1.  Two-airport route resolves origin and destination
  2.  Three-airport route resolves to first/last, middle discarded
  3.  Round-trip route (first == last) resolves, not an error
  4.  Unknown callsign leaves route UNKNOWN, logs unknown_callsign
  5.  No callsign at all — logs no_callsign
  6.  Airport with no IATA — *_iata stays UNKNOWN, rest populates
  7.  Airport code with no matching row — degrades to UNKNOWN, no exception
  8.  Country ISO with no matching row — *_country stays UNKNOWN
  9.  airline_name resolves via routes.airline_code -> airlines.code
  10. Field already populated is not overwritten
  11. flight_number and airline_country are never written by this module
  12. Two module blocks naming source = "vrs" share one data source instance
  13-16. log_level: "none" / "errors" (default) / "verbose" / invalid value
"""

from __future__ import annotations

import json
import sqlite3
import types
from pathlib import Path

import pytest

import modules
import modules.vrs_route as vrs_route_module
from config import DataSourceConfig, ObserverConfig
from data_sources import BaseDataSource, DataSourceContext
from data_sources.vrs_standing_data import SQLiteVrsDb
from modules import ModuleContext, clear_module_pool, get_module
from modules.vrs_route import VrsRouteEnricher, _NOT_FOUND_TTL_SECONDS, _validate_log_level
from schemas.aircraft import (
    Aircraft, AircraftLocation, AircraftMeta, AircraftRaw,
    AircraftRoute, AircraftVector, Airframe,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_aircraft(callsign=None, hex_id="4CA068", registration=None, **route_overrides) -> Aircraft:
    return Aircraft(
        meta      = AircraftMeta(icao_hex=hex_id, ingestor="test", reception_type="adsb_icao"),
        location  = AircraftLocation(),
        direction = AircraftVector(),
        route     = AircraftRoute(callsign=callsign, **route_overrides),
        airframe  = Airframe(registration=registration),
        raw       = AircraftRaw(),
    )


def _build_db(
    tmp_path: Path,
    routes: tuple = (),
    airports: tuple = (),
    countries: tuple = (),
    airlines: tuple = (),
) -> Path:
    db_path = tmp_path / "standing_data.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE routes (callsign TEXT, code TEXT, number INTEGER, "
        "airline_code TEXT, airport_codes TEXT)"
    )
    conn.execute(
        "CREATE TABLE airports (code TEXT, name TEXT, icao TEXT, iata TEXT, "
        "location TEXT, country_iso2 TEXT, latitude REAL, longitude REAL, altitude_feet INTEGER)"
    )
    conn.execute("CREATE TABLE countries (iso TEXT PRIMARY KEY, name TEXT)")
    conn.execute(
        "CREATE TABLE airlines (code TEXT, name TEXT, icao TEXT, iata TEXT, "
        "positioning_flight_pattern TEXT, charter_flight_pattern TEXT)"
    )
    conn.executemany("INSERT INTO routes VALUES (?,?,?,?,?)", routes)
    conn.executemany("INSERT INTO airports VALUES (?,?,?,?,?,?,?,?,?)", airports)
    conn.executemany("INSERT INTO countries VALUES (?,?)", countries)
    conn.executemany("INSERT INTO airlines VALUES (?,?,?,?,?,?)", airlines)
    conn.commit()
    conn.close()
    return db_path


class FakeSource:
    """Stands in for VrsStandingData: no download, a real SQLiteVrsDb over a
    hand-built file."""

    def __init__(self, db_path: Path | None) -> None:
        self.ensure_fresh_calls = 0
        self._db = SQLiteVrsDb(db_path) if db_path else None

    def ensure_fresh(self) -> None:
        self.ensure_fresh_calls += 1

    @property
    def db(self):
        return self._db


# One shared fixture dataset: a normal two-airport route (BAW117,
# EGLL -> KJFK), a three-stop route reducing to first/last, and a round trip.
_ROUTES = (
    ("BAW117",  "BA", 117,  "BAW", "EGLL-KJFK"),
    ("TAP123",  "TP", 123,  "TAP", "LPPT-LEMD-LEBL"),
    ("EAG1RT",  "EA", 1,    "EAG", "EGAA-GCRR-EGAA"),
    ("NOAIR",   "NA", 9,    "NAX", "ZZZZ-YYYY"),        # neither airport exists
    ("NOCTRY",  "NC", 2,    "NCX", "XXQQ-EGLL"),        # origin has an unknown country
    ("NOIATA",  "NI", 3,    "NIX", "EGLL-EGXX"),        # destination has no IATA
)

_AIRPORTS = (
    # code, name, icao, iata, location, country_iso2, lat, lon, alt
    ("LHR", "Heathrow Airport", "EGLL", "LHR", "London", "GB", 51.47, -0.45, 83),
    ("JFK", "John F Kennedy International Airport", "KJFK", "JFK", "New York", "US", 40.64, -73.78, 13),
    ("LIS", "Humberto Delgado Airport", "LPPT", "LIS", "Lisbon", "PT", 38.77, -9.13, 374),
    ("MAD", "Adolfo Suarez Madrid-Barajas Airport", "LEMD", "MAD", "Madrid", "ES", 40.47, -3.56, 1998),
    ("BCN", "Josep Tarradellas Barcelona-El Prat Airport", "LEBL", "BCN", "Barcelona", "ES", 41.29, 2.07, 12),
    ("BEL", "Belfast International Airport", "EGAA", "BFS", "Belfast", "GB", 54.65, -6.22, 268),
    ("GIB", "Gibraltar Airport", "GCRR", "GIB", "Gibraltar", "GI", 36.15, -5.35, 15),
    ("XXQ", "Nowhere Airport", "XXQQ", "XXQ", "Nowhereville", "ZZ", 0.0, 0.0, 0),   # unknown country
    ("XXX", "No IATA Airport", "EGXX", "", "Somewhereville", "GB", 0.0, 0.0, 0),    # no IATA
)

_COUNTRIES = (
    ("GB", "United Kingdom"),
    ("US", "United States"),
    ("PT", "Portugal"),
    ("ES", "Spain"),
    ("GI", "Gibraltar"),
    # 'ZZ' deliberately absent — the unknown-country case
)

_AIRLINES = (
    ("BAW", "British Airways", "BAW", "BA", None, None),
    ("TAP", "TAP Air Portugal", "TAP", "TP", None, None),
    ("EAG", "Positioning Co",  "EAG", "EA", None, None),
    ("NAX", "No Airport Air",  "NAX", "NA", None, None),
    ("NCX", "No Country Air",  "NCX", "NC", None, None),
    ("NIX", "No IATA Air",     "NIX", "NI", None, None),
)


@pytest.fixture
def db_path(tmp_path) -> Path:
    return _build_db(tmp_path, routes=_ROUTES, airports=_AIRPORTS, countries=_COUNTRIES, airlines=_AIRLINES)


@pytest.fixture
def enricher(db_path, tmp_path) -> VrsRouteEnricher:
    return VrsRouteEnricher(source=FakeSource(db_path), log_dir=tmp_path / "log")


# ===========================================================================
# 1. Two-airport route
# ===========================================================================

def test_two_airport_route_resolves_origin_and_destination(enricher):
    aircraft = [_make_aircraft(callsign="BAW117")]
    result = enricher.process(aircraft)
    r = result[0].route
    assert r.origin_iata == "LHR"
    assert r.origin_name == "Heathrow Airport"
    assert r.origin_municipality == "London"
    assert r.origin_country == "United Kingdom"
    assert r.destination_iata == "JFK"
    assert r.destination_name == "John F Kennedy International Airport"
    assert r.destination_municipality == "New York"
    assert r.destination_country == "United States"
    assert r.airline_name == "British Airways"


# ===========================================================================
# 2. Three-airport route — first/last, middle discarded
# ===========================================================================

def test_three_airport_route_reduces_to_first_and_last(enricher):
    aircraft = [_make_aircraft(callsign="TAP123")]
    result = enricher.process(aircraft)
    r = result[0].route
    assert r.origin_iata == "LIS"          # LPPT
    assert r.destination_iata == "BCN"     # LEBL, not MAD (the discarded middle stop)
    assert r.origin_name != "Adolfo Suarez Madrid-Barajas Airport"
    assert r.destination_name != "Adolfo Suarez Madrid-Barajas Airport"


# ===========================================================================
# 3. Round trip — first == last, not an error
# ===========================================================================

def test_round_trip_route_resolves_with_origin_equal_to_destination(enricher):
    aircraft = [_make_aircraft(callsign="EAG1RT")]
    result = enricher.process(aircraft)
    r = result[0].route
    assert r.origin_iata == r.destination_iata == "BFS"
    assert r.origin_name == r.destination_name == "Belfast International Airport"


# ===========================================================================
# 4. Unknown callsign
# ===========================================================================

def test_unknown_callsign_leaves_route_unknown_and_logs(enricher, tmp_path):
    aircraft = [_make_aircraft(callsign="ZZZ999", hex_id="ABCDEF")]
    result = enricher.process(aircraft)
    r = result[0].route
    assert r.origin_iata is None
    assert r.destination_iata is None

    log_path = tmp_path / "log" / "unresolved.jsonl"
    lines = [json.loads(l) for l in log_path.read_text().splitlines()]
    assert len(lines) == 1
    assert lines[0]["reason"] == "unknown_callsign"
    assert lines[0]["callsign"] == "ZZZ999"
    assert lines[0]["hex"] == "ABCDEF"


# ===========================================================================
# 5. No callsign at all
# ===========================================================================

def test_no_callsign_logs_no_callsign(enricher, tmp_path):
    aircraft = [_make_aircraft(callsign=None, hex_id="ABCDEF")]
    result = enricher.process(aircraft)
    assert result[0].route.origin_iata is None

    log_path = tmp_path / "log" / "unresolved.jsonl"
    lines = [json.loads(l) for l in log_path.read_text().splitlines()]
    assert len(lines) == 1
    assert lines[0]["reason"] == "no_callsign"
    assert lines[0]["callsign"] is None


# ===========================================================================
# 6. Airport with no IATA
# ===========================================================================

def test_airport_with_no_iata_leaves_iata_unknown_but_fills_the_rest(enricher):
    aircraft = [_make_aircraft(callsign="NOIATA")]
    result = enricher.process(aircraft)
    r = result[0].route
    assert r.destination_iata is None
    assert r.destination_name == "No IATA Airport"
    assert r.destination_municipality == "Somewhereville"
    assert r.destination_country == "United Kingdom"


# ===========================================================================
# 7. Airport code with no matching row at all
# ===========================================================================

def test_airport_with_no_matching_row_degrades_to_unknown(enricher):
    aircraft = [_make_aircraft(callsign="NOAIR")]
    result = enricher.process(aircraft)   # must not raise
    r = result[0].route
    assert r.origin_iata is None
    assert r.origin_name is None
    assert r.destination_iata is None
    assert r.destination_name is None
    # airline_name still resolves independently of the airport miss
    assert r.airline_name == "No Airport Air"


# ===========================================================================
# 8. Country with no matching row
# ===========================================================================

def test_country_with_no_matching_row_leaves_country_unknown(enricher):
    aircraft = [_make_aircraft(callsign="NOCTRY")]
    result = enricher.process(aircraft)
    r = result[0].route
    assert r.origin_name == "Nowhere Airport"   # airport itself still resolves
    assert r.origin_country is None             # but its country does not
    assert r.destination_country == "United Kingdom"


# ===========================================================================
# 9. airline_name via routes.airline_code -> airlines.code
# ===========================================================================

def test_airline_name_resolves_via_airline_code(enricher):
    aircraft = [_make_aircraft(callsign="BAW117")]
    result = enricher.process(aircraft)
    assert result[0].route.airline_name == "British Airways"


# ===========================================================================
# 10. Guarded writes — a field already populated is not overwritten
# ===========================================================================

def test_preset_fields_are_not_overwritten(enricher):
    aircraft = [_make_aircraft(
        callsign="BAW117",
        origin_iata="XXX",
        airline_name="Preset Airline",
    )]
    result = enricher.process(aircraft)
    r = result[0].route
    assert r.origin_iata == "XXX"
    assert r.airline_name == "Preset Airline"
    # Unset fields on the same aircraft still fill normally
    assert r.destination_iata == "JFK"


# ===========================================================================
# 11. flight_number / airline_country are never written
# ===========================================================================

def test_flight_number_and_airline_country_are_never_written(enricher):
    aircraft = [_make_aircraft(callsign="BAW117")]
    result = enricher.process(aircraft)
    r = result[0].route
    assert r.flight_number is None
    assert r.airline_country is None


# ===========================================================================
# 12. Two module blocks naming source = "vrs" share one data source instance
# ===========================================================================

class _FakeVrsSource(BaseDataSource):
    def __init__(self, cfg: dict, ctx: DataSourceContext) -> None:
        self._dir = ctx.source_dir

    def ensure_fresh(self) -> None:
        pass

    @property
    def directory(self) -> Path:
        return self._dir

    @property
    def db(self):
        return None


@pytest.fixture
def fake_vrs_source_type(monkeypatch):
    module = types.ModuleType("data_sources.fake_vrs_source")
    module.KEYS = set()
    module.get = lambda cfg, ctx: _FakeVrsSource(cfg, ctx)
    import sys
    monkeypatch.setitem(sys.modules, "data_sources.fake_vrs_source", module)
    return module


@pytest.fixture
def reset_pools(tmp_path, monkeypatch):
    from config import config as squawk_config
    from data_sources import clear_data_source_pool

    clear_module_pool()
    clear_data_source_pool()
    monkeypatch.setattr(squawk_config.squawk, "data_dir", str(tmp_path))
    monkeypatch.setattr(squawk_config, "data_sources", {
        "vrs": DataSourceConfig(name="vrs", type="fake_vrs_source", cfg={"type": "fake_vrs_source"}),
    })
    yield
    clear_module_pool()
    clear_data_source_pool()


def test_two_module_blocks_sharing_source_share_one_data_source(
    reset_pools, fake_vrs_source_type,
):
    a = get_module("vrs_route_a", {"type": "vrs_route", "source": "vrs"})
    b = get_module("vrs_route_b", {"type": "vrs_route", "source": "vrs"})
    assert a is not b
    assert a._source is b._source


# ===========================================================================
# 13-16. log_level
# ===========================================================================

def test_log_level_none_prints_nothing_on_hit_or_miss(db_path, tmp_path, capsys):
    enricher = VrsRouteEnricher(source=FakeSource(db_path), log_dir=tmp_path / "log", log_level="none")
    enricher.process([_make_aircraft(callsign="BAW117")])
    enricher.process([_make_aircraft(callsign="ZZZ999")])
    assert capsys.readouterr().out == ""


def test_log_level_errors_prints_miss_but_not_hit(db_path, tmp_path, capsys):
    enricher = VrsRouteEnricher(source=FakeSource(db_path), log_dir=tmp_path / "log", log_level="errors")
    enricher.process([_make_aircraft(callsign="BAW117")])
    assert capsys.readouterr().out == ""

    enricher.process([_make_aircraft(callsign="ZZZ999")])
    out = capsys.readouterr().out
    assert "ZZZ999" in out
    assert "unknown" in out


def test_log_level_defaults_to_errors(db_path, tmp_path):
    enricher = VrsRouteEnricher(source=FakeSource(db_path), log_dir=tmp_path / "log")
    assert enricher._log_level == "errors"


def test_log_level_verbose_prints_hit_and_miss_with_expected_format(db_path, tmp_path, capsys):
    enricher = VrsRouteEnricher(source=FakeSource(db_path), log_dir=tmp_path / "log", log_level="verbose")
    enricher.process([_make_aircraft(callsign="BAW117")])
    out = capsys.readouterr().out
    assert "vrs_route: callsign BAW117 returned LHR - JFK" in out

    enricher.process([_make_aircraft(callsign="ZZZ999")])
    out = capsys.readouterr().out
    assert "vrs_route: callsign ZZZ999 unknown — no route" in out


def test_invalid_log_level_raises_at_construction():
    with pytest.raises(ValueError):
        _validate_log_level("verbose_please")


def test_validate_log_level_defaults_when_absent():
    assert _validate_log_level(None) == "errors"


@pytest.mark.parametrize("level", ["none", "errors", "verbose"])
def test_validate_log_level_accepts_the_three_literals(level):
    assert _validate_log_level(level) == level


# ===========================================================================
# ensure_fresh() is called from process(), not only at construction — see
# docs/data-sources-guide.md's warning about this exact bug.
# ===========================================================================

def test_process_calls_ensure_fresh(db_path, tmp_path):
    source = FakeSource(db_path)
    enricher = VrsRouteEnricher(source=source, log_dir=tmp_path / "log")
    enricher.process([])
    assert source.ensure_fresh_calls == 1
    enricher.process([])
    assert source.ensure_fresh_calls == 2


def test_no_db_yet_leaves_aircraft_unchanged(tmp_path):
    enricher = VrsRouteEnricher(source=FakeSource(None), log_dir=tmp_path / "log")
    aircraft = [_make_aircraft(callsign="BAW117")]
    result = enricher.process(aircraft)
    assert result[0].route.origin_iata is None


# ===========================================================================
# Negative-result cache (brief-vrs-route-not-found-cache.md)
# ===========================================================================

class _CountingDb:
    """Wraps a real SQLiteVrsDb and counts get_route() calls."""

    def __init__(self, db_path: Path) -> None:
        self._inner = SQLiteVrsDb(db_path)
        self.calls = 0

    def get_route(self, callsign: str):
        self.calls += 1
        return self._inner.get_route(callsign)

    def get_airport(self, icao: str):
        return self._inner.get_airport(icao)

    def get_country(self, iso2: str):
        return self._inner.get_country(iso2)

    def get_airline(self, code: str):
        return self._inner.get_airline(code)


class _CountingFakeSource:
    def __init__(self, db) -> None:
        self.ensure_fresh_calls = 0
        self._db = db

    def ensure_fresh(self) -> None:
        self.ensure_fresh_calls += 1

    @property
    def db(self):
        return self._db


class _FakeTime:
    """Replaces the `time` module vrs_route calls `time.time()` through.

    Patched in via `monkeypatch.setattr(vrs_route_module, "time", _FakeTime(box))`
    — mirrors modules/test_tar1090_db.py's own _FakeTime.
    """

    def __init__(self, box: list[float]) -> None:
        self._box = box

    def time(self) -> float:
        return self._box[0]


def _unresolved_lines(tmp_path: Path) -> list[dict]:
    path = tmp_path / "log" / "unresolved.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines()]


def test_first_miss_queries_db_prints_once_and_logs_once(db_path, tmp_path, capsys):
    counting_db = _CountingDb(db_path)
    enricher = VrsRouteEnricher(source=_CountingFakeSource(counting_db), log_dir=tmp_path / "log")

    enricher.process([_make_aircraft(callsign="ZZZ999", hex_id="ABCDEF")])

    assert counting_db.calls == 1
    out = capsys.readouterr().out
    assert "ZZZ999" in out and "unknown" in out

    lines = _unresolved_lines(tmp_path)
    assert len(lines) == 1
    assert lines[0]["reason"] == "unknown_callsign"


def test_same_callsign_within_ttl_is_a_complete_skip(db_path, tmp_path, capsys, monkeypatch):
    t = [1_700_000_000.0]
    monkeypatch.setattr(vrs_route_module, "time", _FakeTime(t))

    counting_db = _CountingDb(db_path)
    enricher = VrsRouteEnricher(source=_CountingFakeSource(counting_db), log_dir=tmp_path / "log")

    enricher.process([_make_aircraft(callsign="ZZZ999", hex_id="ABCDEF")])
    assert counting_db.calls == 1
    capsys.readouterr()   # discard cycle-1 output

    t[0] += 60   # well within the 1-hour TTL
    enricher.process([_make_aircraft(callsign="ZZZ999", hex_id="ABCDEF")])

    assert counting_db.calls == 1          # no new DB call
    assert capsys.readouterr().out == ""   # no console line, even at default "errors"
    assert len(_unresolved_lines(tmp_path)) == 1   # no new log line


def test_ttl_expired_re_attempts_in_full(db_path, tmp_path, capsys, monkeypatch):
    t = [1_700_000_000.0]
    monkeypatch.setattr(vrs_route_module, "time", _FakeTime(t))

    counting_db = _CountingDb(db_path)
    enricher = VrsRouteEnricher(source=_CountingFakeSource(counting_db), log_dir=tmp_path / "log")

    enricher.process([_make_aircraft(callsign="ZZZ999", hex_id="ABCDEF")])
    assert counting_db.calls == 1
    capsys.readouterr()

    t[0] += _NOT_FOUND_TTL_SECONDS + 1   # past the TTL
    enricher.process([_make_aircraft(callsign="ZZZ999", hex_id="ABCDEF")])

    assert counting_db.calls == 2
    out = capsys.readouterr().out
    assert "ZZZ999" in out
    assert len(_unresolved_lines(tmp_path)) == 2


def test_key_cleared_on_subsequent_hit(tmp_path, monkeypatch):
    # BAW117 starts out absent from the routes table entirely. A lookup
    # while still within the TTL window is a complete skip by design (that's
    # the whole point of the cache), so a mid-window dataset refresh isn't
    # discovered until the TTL naturally expires and the key is retried —
    # advance the fake clock past it before the second attempt.
    t = [1_700_000_000.0]
    monkeypatch.setattr(vrs_route_module, "time", _FakeTime(t))

    db_path = _build_db(tmp_path, routes=(), airports=_AIRPORTS, countries=_COUNTRIES, airlines=_AIRLINES)
    counting_db = _CountingDb(db_path)
    enricher = VrsRouteEnricher(source=_CountingFakeSource(counting_db), log_dir=tmp_path / "log")

    enricher.process([_make_aircraft(callsign="BAW117")])
    assert counting_db.calls == 1
    assert "BAW117" in enricher._not_found

    # Simulate a dataset refresh: the route now exists.
    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT INTO routes VALUES (?,?,?,?,?)", ("BAW117", "BA", 117, "BAW", "EGLL-KJFK"))
    conn.commit()
    conn.close()

    t[0] += _NOT_FOUND_TTL_SECONDS + 1
    result = enricher.process([_make_aircraft(callsign="BAW117")])
    assert counting_db.calls == 2                    # the TTL-expired key was retried in full
    assert result[0].route.origin_iata == "LHR"       # hit applied normally
    assert "BAW117" not in enricher._not_found        # not left stale alongside the fresh hit


def test_no_callsign_gets_the_same_three_effect_suppression_keyed_by_hex(
    db_path, tmp_path, capsys, monkeypatch,
):
    t = [1_700_000_000.0]
    monkeypatch.setattr(vrs_route_module, "time", _FakeTime(t))

    counting_db = _CountingDb(db_path)
    enricher = VrsRouteEnricher(source=_CountingFakeSource(counting_db), log_dir=tmp_path / "log")

    enricher.process([_make_aircraft(callsign=None, hex_id="ABCDEF")])
    capsys.readouterr()
    assert len(_unresolved_lines(tmp_path)) == 1
    assert counting_db.calls == 0   # no_callsign never reaches get_route in the first place

    t[0] += 60
    enricher.process([_make_aircraft(callsign=None, hex_id="ABCDEF")])
    assert capsys.readouterr().out == ""
    assert len(_unresolved_lines(tmp_path)) == 1   # no new line for the same hex

    # A different hex with no callsign is an independent key, not suppressed.
    enricher.process([_make_aircraft(callsign=None, hex_id="112233")])
    assert len(_unresolved_lines(tmp_path)) == 2


def test_two_different_callsigns_have_independent_ttl_windows(db_path, tmp_path, monkeypatch):
    t = [1_700_000_000.0]
    monkeypatch.setattr(vrs_route_module, "time", _FakeTime(t))

    counting_db = _CountingDb(db_path)
    enricher = VrsRouteEnricher(source=_CountingFakeSource(counting_db), log_dir=tmp_path / "log")

    enricher.process([_make_aircraft(callsign="ZZZ111")])
    enricher.process([_make_aircraft(callsign="ZZZ222")])
    assert counting_db.calls == 2

    t[0] += 60
    enricher.process([_make_aircraft(callsign="ZZZ111")])
    enricher.process([_make_aircraft(callsign="ZZZ222")])
    assert counting_db.calls == 2   # both still independently suppressed

    t[0] += _NOT_FOUND_TTL_SECONDS
    enricher.process([_make_aircraft(callsign="ZZZ111")])
    assert counting_db.calls == 3
    enricher.process([_make_aircraft(callsign="ZZZ222")])
    assert counting_db.calls == 4


def test_sweep_drops_expired_entries_over_many_cycles(db_path, tmp_path, monkeypatch):
    t = [1_700_000_000.0]
    monkeypatch.setattr(vrs_route_module, "time", _FakeTime(t))

    counting_db = _CountingDb(db_path)
    enricher = VrsRouteEnricher(source=_CountingFakeSource(counting_db), log_dir=tmp_path / "log")

    enricher.process([_make_aircraft(callsign="ZZZ999")])
    assert "ZZZ999" in enricher._not_found

    # A long-running process ticking every 5s, well under the TTL — the
    # entry must not be swept prematurely.
    for _ in range(600):
        t[0] += 5
        enricher.process([])
    assert "ZZZ999" in enricher._not_found

    t[0] += _NOT_FOUND_TTL_SECONDS
    enricher.process([])   # sweep runs even with no aircraft this cycle
    assert "ZZZ999" not in enricher._not_found
