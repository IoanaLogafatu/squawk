"""
ingestor/personal_adsb/converter.py

Converts a raw readsb/tar1090 aircraft record into a Squawk Aircraft object.

The full source payload is preserved verbatim in AircraftRaw — nothing is
filtered out. Mapped fields are copied into typed schema fields; the raw
dict is stored as-is alongside them.

Usage:
    from ingestor.personal_adsb.converter import convert_aircraft

    aircraft = convert_aircraft(raw_record, observed_at=datetime(...))
"""

from __future__ import annotations

from datetime import datetime

from schemas.aircraft import (
    UNKNOWN,
    Aircraft,
    AircraftLocation,
    AircraftMeta,
    AircraftRaw,
    AircraftRoute,
    AircraftVector,
    Airframe,
)


# ---------------------------------------------------------------------------
# Field transforms
# ---------------------------------------------------------------------------

def _altitude(value: object) -> int | None:
    """
    alt_baro is polymorphic: an integer (feet) in flight, or the string
    "ground" when the aircraft is on the ground. Normalise to integer; 0 = ground.
    """
    if value == "ground":
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    return UNKNOWN


def _callsign(value: object) -> str | None:
    """
    Callsigns are 8 characters, space-padded on the right. Strip whitespace.
    Return None (UNKNOWN) if the result is empty.
    """
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else UNKNOWN
    return UNKNOWN


def _icao_hex(value: object) -> str | None:
    """Normalise to uppercase. Avoids mixed-case duplicates in lookups."""
    if isinstance(value, str):
        return value.upper()
    return UNKNOWN


# ---------------------------------------------------------------------------
# Main converter
# ---------------------------------------------------------------------------

def convert_aircraft(
    raw: dict,
    observed_at: datetime | None = None,
) -> Aircraft | None:
    """
    Convert one raw readsb aircraft record into a Squawk Aircraft object.

    Returns None if the record has no usable ICAO hex — those records
    are malformed and should be discarded.

    Args:
        raw:         One entry from the readsb aircraft.json `aircraft` array.
        observed_at: UTC datetime of the last message from this aircraft,
                     computed by the ingestor as snapshot_now - seen_seconds.
    """

    # ICAO hex is the minimum viable record — nothing works without it
    icao_hex = _icao_hex(raw.get("hex"))
    if not icao_hex:
        return None

    meta = AircraftMeta(
        icao_hex       = icao_hex,
        ingestor       = "personal_adsb",
        observed_at    = observed_at,
        reception_type = raw.get("type", UNKNOWN),
    )

    location = AircraftLocation(
        latitude        = raw.get("lat",   UNKNOWN),
        longitude       = raw.get("lon",   UNKNOWN),
        altitude_feet   = _altitude(raw.get("alt_baro")),
        distance_nm     = raw.get("r_dst", UNKNOWN),
        bearing_degrees = raw.get("r_dir", UNKNOWN),
        seen_seconds    = raw.get("seen",  UNKNOWN),
    )

    direction = AircraftVector(
        ground_speed_knots = raw.get("gs",       UNKNOWN),
        track_degrees      = raw.get("track",    UNKNOWN),
        vertical_rate_fpm  = raw.get("baro_rate",UNKNOWN),
    )

    route = AircraftRoute(
        callsign         = _callsign(raw.get("flight")),
        squawk_code      = raw.get("squawk", UNKNOWN),
        origin_iata      = UNKNOWN,
        destination_iata = UNKNOWN,
        flight_number    = UNKNOWN,
    )

    airframe = Airframe(
        registration  = raw.get("r",    UNKNOWN),
        aircraft_type = raw.get("desc", UNKNOWN),
        operator      = UNKNOWN,
    )

    return Aircraft(
        meta      = meta,
        location  = location,
        direction = direction,
        route     = route,
        airframe  = airframe,
        raw       = AircraftRaw(payload=dict(raw)),
    )
