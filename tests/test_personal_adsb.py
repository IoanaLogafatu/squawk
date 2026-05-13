"""
tests/test_personal_adsb.py

pytest tests for the PersonalADSB ingestor.

Covers:
  1. Merge logic  (_merge_snapshots)
  2. Converter    (convert_aircraft)
  3. Schema contract (every envelope output is valid)
  4. Edge case    (407e82 data quality note)
  5. JSON serialisation (SquawkEncoder round trip)

No network calls. Fixtures from tests/fixtures/adsb1.json and adsb2.json.
"""

import json
from dataclasses import fields as dc_fields
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ingestor.personal_adsb.converter import convert_aircraft
from ingestor.personal_adsb.ingestor import _build_envelope, _merge_snapshots
from schemas.encoder import SquawkEncoder
from schemas.aircraft import (
    UNKNOWN,
    AircraftLocation,
    AircraftMeta,
    AircraftRoute,
    AircraftVector,
    Airframe,
    ReceiverStatus,
)

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def snap1():
    return json.loads((FIXTURES / "adsb1.json").read_text())


@pytest.fixture(scope="module")
def snap2():
    return json.loads((FIXTURES / "adsb2.json").read_text())


@pytest.fixture(scope="module")
def merged_records(snap1, snap2):
    return _merge_snapshots([
        ("receiver1", snap1),
        ("receiver2", snap2),
    ])


@pytest.fixture(scope="module")
def merged_by_hex(merged_records):
    """Aircraft objects keyed by uppercase ICAO hex after conversion."""
    result = {}
    for raw_record, observed_at in merged_records:
        ac = convert_aircraft(raw_record, observed_at=observed_at)
        if ac:
            result[ac.meta.icao_hex] = ac
    return result


@pytest.fixture(scope="module")
def envelope(snap1, snap2):
    merged = _merge_snapshots([
        ("receiver1", snap1),
        ("receiver2", snap2),
    ])
    status = [
        ReceiverStatus(name="receiver1", healthy=True,  last_seen=datetime.now(timezone.utc)),
        ReceiverStatus(name="receiver2", healthy=False, last_seen=None, error="Connection refused"),
    ]
    return _build_envelope(merged, status)


def _find_raw(snapshot: dict, hex_id: str) -> dict:
    for aircraft in snapshot.get("aircraft", []):
        if aircraft.get("hex", "").upper() == hex_id.upper():
            return aircraft
    raise KeyError(f"{hex_id} not found in snapshot")


# ===========================================================================
# 1. Merge tests
# ===========================================================================

def test_merge_total_count(merged_records):
    assert len(merged_records) == 22


@pytest.mark.parametrize("hex_id,winning_seen", [
    # receiver1 wins — its snapshot is 4.3s newer, so its absolute observed_at dominates
    ("4D226A", 0.2),   # r1=0.2, r2=1.1
    ("40649F", 0.0),   # r1=0.0, r2=0.4
    ("4D24D8", 0.2),   # r1=0.2, r2=0.2 — tie in seen; r1 wins on newer snapshot
    ("407E82", 0.4),   # r1=0.4, r2=1.7
    ("405638", 0.2),   # r1=0.2, r2=0.0 — r2 seen=0.0 but snapshot is 4.3s older; r1 wins
    ("4071D7", 2.7),   # r1=2.7, r2=0.2 — r2 seen=0.2 but snapshot is 4.3s older; r1 wins
    # receiver2 wins — its observation is more recent in absolute time
    ("4009DA", 0.1),   # r1=11.6, r2=0.1
    ("4CAD7D", 0.2),   # r1=12.7, r2=0.2
    ("4D21F0", 0.4),   # r1=5.5,  r2=0.4
    ("4D2387", 0.4),   # r1=18.1, r2=0.4
])
def test_merge_winner_by_observed_at(merged_by_hex, hex_id, winning_seen):
    ac = merged_by_hex[hex_id]
    assert ac.location.seen_seconds == winning_seen


# ===========================================================================
# 2. Converter tests
# ===========================================================================

def test_us_operator_is_unknown(snap1):
    # ownOp is not mapped — operator is left blank for a plugin to fill
    raw = _find_raw(snap1, "a8ac35")
    ac = convert_aircraft(raw)
    assert ac.airframe.operator is None


def test_us_ownop_preserved_in_raw(snap2):
    # ownOp passes through to raw.payload so a plugin can pick it up
    raw = _find_raw(snap2, "abc1da")
    ac = convert_aircraft(raw)
    assert ac.raw.payload.get("ownOp") == "FEDERAL EXPRESS CORP"


def test_last_position_only_no_current_position(snap1):
    # 440117 (OE-LUF) has lastPosition but no live lat/lon/r_dst
    raw = _find_raw(snap1, "440117")
    ac = convert_aircraft(raw)
    assert ac.location.latitude is None
    assert ac.location.longitude is None
    assert ac.location.distance_nm is None


def test_nearly_empty_mode_s(snap2):
    # 8964bd (A6-BLM) is mode_s with only seen and rssi
    raw = _find_raw(snap2, "8964bd")
    ac = convert_aircraft(raw)
    assert ac.location.latitude is None
    assert ac.location.longitude is None
    assert ac.location.altitude_feet is None
    assert ac.direction.ground_speed_knots is None
    assert ac.route.squawk_code is None
    assert ac.location.seen_seconds == 36.1


def test_callsign_stripping(snap1):
    raw = _find_raw(snap1, "407f42")
    assert raw.get("flight") == "EAG7FT  "  # confirm trailing spaces in source
    ac = convert_aircraft(raw)
    assert ac.route.callsign == "EAG7FT"


def test_icao_hex_uppercasing(snap1):
    raw = _find_raw(snap1, "4cad3d")
    assert raw["hex"] == "4cad3d"  # confirm lowercase in source
    ac = convert_aircraft(raw)
    assert ac.meta.icao_hex == "4CAD3D"


def test_raw_payload_passthrough(snap1):
    # 4d226a has nic, nac_p, rssi — all should be in raw.payload
    raw = _find_raw(snap1, "4d226a")
    ac = convert_aircraft(raw)

    for field_name in ("nic", "nac_p", "rssi"):
        assert field_name in raw, f"precondition: source record missing {field_name}"
        assert field_name in ac.raw.payload
        assert field_name not in vars(ac.meta)
        assert field_name not in vars(ac.location)
        assert field_name not in vars(ac.direction)
        assert field_name not in vars(ac.route)
        assert field_name not in vars(ac.airframe)


def test_missing_hex_returns_none():
    assert convert_aircraft({}) is None


def test_alt_baro_ground():
    raw = {"hex": "aabbcc", "seen": 1.0, "alt_baro": "ground"}
    ac = convert_aircraft(raw)
    assert ac.location.altitude_feet == 0


# ===========================================================================
# 3. Contract tests — every envelope output is schema-valid
# ===========================================================================

def test_envelope_source(envelope):
    assert envelope.source == "PersonalADSB"


def test_envelope_aircraft_count_matches_list(envelope):
    assert envelope.aircraft_count == len(envelope.aircraft)


def test_envelope_timestamp_is_timezone_aware(envelope):
    assert isinstance(envelope.timestamp, datetime)
    assert envelope.timestamp.tzinfo is not None


def test_all_aircraft_meta_icao_hex_nonempty(envelope):
    for ac in envelope.aircraft:
        assert isinstance(ac.meta.icao_hex, str) and ac.meta.icao_hex


def test_all_aircraft_meta_ingestor(envelope):
    for ac in envelope.aircraft:
        assert ac.meta.ingestor == "personal_adsb"


def test_all_aircraft_meta_reception_type_string_or_none(envelope):
    for ac in envelope.aircraft:
        rtype = ac.meta.reception_type
        assert rtype is None or isinstance(rtype, str), \
            f"{ac.meta.icao_hex}: reception_type={rtype!r}"


def test_all_aircraft_sections_present(envelope):
    for ac in envelope.aircraft:
        assert ac.location is not None
        assert ac.direction is not None
        assert ac.route is not None
        assert ac.airframe is not None
        assert ac.raw is not None


def test_all_aircraft_seen_seconds_is_float(envelope):
    for ac in envelope.aircraft:
        assert isinstance(ac.location.seen_seconds, float), \
            f"{ac.meta.icao_hex}: seen_seconds={ac.location.seen_seconds!r}"


def test_all_aircraft_no_extra_fields(envelope):
    expected_meta      = {f.name for f in dc_fields(AircraftMeta)}
    expected_location  = {f.name for f in dc_fields(AircraftLocation)}
    expected_direction = {f.name for f in dc_fields(AircraftVector)}
    expected_route     = {f.name for f in dc_fields(AircraftRoute)}
    expected_airframe  = {f.name for f in dc_fields(Airframe)}

    for ac in envelope.aircraft:
        assert set(vars(ac.meta))      == expected_meta,      ac.meta.icao_hex
        assert set(vars(ac.location))  == expected_location,  ac.meta.icao_hex
        assert set(vars(ac.direction)) == expected_direction, ac.meta.icao_hex
        assert set(vars(ac.route))     == expected_route,     ac.meta.icao_hex
        assert set(vars(ac.airframe))  == expected_airframe,  ac.meta.icao_hex


def test_receiver_status_is_list(envelope):
    assert isinstance(envelope.receiver_status, list)


def test_receiver_status_entries(envelope):
    for rs in envelope.receiver_status:
        assert isinstance(rs.name, str) and rs.name
        assert isinstance(rs.healthy, bool)
        if rs.healthy:
            assert isinstance(rs.last_seen, datetime) and rs.last_seen.tzinfo is not None
        else:
            assert rs.last_seen is None
        assert rs.error is None or isinstance(rs.error, str)


# ===========================================================================
# 4. Edge case — 407e82 data quality note
# ===========================================================================

def test_407e82_mode_s_wins_over_adsb_icao(merged_by_hex):
    # receiver1 saw 407e82 as mode_s (seen=0.4); receiver2 as adsb_icao (seen=1.7).
    # receiver1's snapshot is newer in absolute time — its record wins.
    ac = merged_by_hex["407E82"]
    assert ac.meta.icao_hex == "407E82"
    assert ac.meta.reception_type == "mode_s"
    assert ac.location.latitude is None


# ===========================================================================
# 5. JSON serialisation — SquawkEncoder round trip
# ===========================================================================

def test_json_round_trip(envelope):
    serialised = json.dumps(envelope, cls=SquawkEncoder)
    data = json.loads(serialised)

    assert data["source"] == envelope.source
    assert data["aircraft_count"] == envelope.aircraft_count
    assert datetime.fromisoformat(data["timestamp"]) == envelope.timestamp
    assert isinstance(data["receiver_status"], list)
