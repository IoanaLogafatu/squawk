"""
schemas/aircraft.py

Canonical data schema for the Squawk aircraft ingestor.

All fields are always present. Fields the source cannot populate are set to
UNKNOWN (None). Downstream plugins use None as a signal that they should
attempt to fill the field.

Sections:
    AircraftMeta     — identity and provenance (ingestor, observed_at)
    AircraftLocation — where the aircraft is (position, altitude, range)
    AircraftVector   — how it is moving (speed, track, climb rate)
    AircraftRoute    — the flight it is operating (callsign, squawk, origin/dest)
    Airframe         — the physical aircraft (registration, type, operator)
    AircraftRaw      — full unmodified source payload

Envelope:
    SquawkEnvelope   — one object per poll; contains all aircraft seen
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


# Sentinel — explicit alias so intent is clear throughout the codebase
UNKNOWN = None


# ---------------------------------------------------------------------------
# Receiver status
# ---------------------------------------------------------------------------

@dataclass
class ReceiverStatus:
    """Health of one receiver as recorded during a poll cycle."""

    name:      str
    healthy:   bool
    last_seen: Optional[datetime] = UNKNOWN  # UTC time of last successful poll
    error:     Optional[str]      = UNKNOWN  # Error message if unhealthy


# ---------------------------------------------------------------------------
# Per-aircraft sections
# ---------------------------------------------------------------------------

@dataclass
class AircraftMeta:
    """
    Identity and provenance. Set by the ingestor, not the source.

    icao_hex       — 24-bit ICAO address, globally unique per transponder.
                     Ties all records for the same airframe together across
                     receivers and enrichment sources. Analogous to a VIN —
                     but assigned to the transponder box, not the airframe,
                     so it moves if the transponder is swapped.
    ingestor       — Which ingestor produced this record, e.g. "personal_adsb".
    observed_at    — UTC time the last message from this aircraft was received.
                     Computed as snapshot.now - seen_seconds. Storage merge key:
                     a record is only written to disk if observed_at is more
                     recent than the existing on-disk record for this hex.
    reception_type — How the data was obtained. Values from readsb:
                       adsb_icao   : ADS-B from an ICAO-addressed transponder (most common)
                       adsb_icao_nt: ADS-B, no timing
                       adsr_icao   : ADS-B rebroadcast
                       mlat        : Multilateration (position derived, lower integrity)
                       mode_s      : Mode-S only — no position
                       tisb_icao   : TIS-B (ground-relayed traffic info)
                     Note: ADS-B carries no cryptographic authentication —
                     the hex is trusted by convention, not verified.
    """

    icao_hex:       str                     # e.g. "4CA7B2"
    ingestor:       str                     # e.g. "personal_adsb"
    observed_at:    Optional[datetime] = UNKNOWN   # UTC time of last message; storage merge key
    reception_type: Optional[str]      = UNKNOWN   # e.g. "adsb_icao", "mlat"


@dataclass
class AircraftLocation:
    """
    Where the aircraft is right now.

    altitude_feet  — Barometric altitude. Source field alt_baro is polymorphic:
                     integer (feet) in flight, or the string "ground" when the
                     aircraft is on the ground. Stored here as integer; 0 = ground.
    distance_nm    — Distance from the receiver in nautical miles (readsb r_dst).
    bearing_degrees — Bearing from receiver to aircraft (readsb r_dir), 0–359.
    seen_seconds   — Seconds since any message was received from this aircraft.
    """

    latitude:        Optional[float] = UNKNOWN
    longitude:       Optional[float] = UNKNOWN
    altitude_feet:   Optional[int]   = UNKNOWN   # Barometric; 0 = on ground
    distance_nm:     Optional[float] = UNKNOWN   # Distance from receiver (r_dst)
    bearing_degrees: Optional[float] = UNKNOWN   # Bearing from receiver (r_dir), 0–359
    seen_seconds:    Optional[float] = UNKNOWN   # Seconds since last message


@dataclass
class AircraftVector:
    """How the aircraft is moving."""

    ground_speed_knots: Optional[float] = UNKNOWN
    track_degrees:      Optional[float] = UNKNOWN   # 0–359, true north
    vertical_rate_fpm:  Optional[int]   = UNKNOWN   # +ve = climbing, -ve = descending


@dataclass
class AircraftRoute:
    """
    The flight being operated. Stable while the aircraft is in range.

    squawk_code    — 4-digit octal code assigned by ATC per airspace entry.
    callsign       — ICAO flight number / radio callsign.
    origin/dest    — Not in ADS-B — enrichment plugins only.
    flight_number  — Not in ADS-B — enrichment plugins only.
    """

    callsign:         Optional[str] = UNKNOWN   # ICAO flight number / radio callsign
    squawk_code:      Optional[str] = UNKNOWN   # 4-digit octal transponder code
    origin_iata:      Optional[str] = UNKNOWN   # Departure airport, e.g. "LHR"
    destination_iata: Optional[str] = UNKNOWN   # Arrival airport, e.g. "JFK"
    flight_number:    Optional[str] = UNKNOWN   # Commercial flight number, e.g. "BA117"


@dataclass
class Airframe:
    """
    The physical aircraft. Long-lived data tied to the airframe (or its transponder).

    operator — Populated from FAA registry data only. US-registered aircraft
               via PersonalADSB; European and other registries require separate
               enrichment plugins.
    """

    registration:  Optional[str] = UNKNOWN   # Tail number, e.g. "G-EUPT"
    aircraft_type: Optional[str] = UNKNOWN   # ICAO type code, e.g. "A320"
    operator:      Optional[str] = UNKNOWN   # Airline or owner name


@dataclass
class AircraftRaw:
    """
    Full unmodified payload from the source.
    Lets consumers or plugins access fields not mapped into the Squawk schema —
    for example: wind data, integrity fields (NIC/NAC/SIL), nav modes, RSSI.
    """

    payload: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Per-aircraft record
# ---------------------------------------------------------------------------

@dataclass
class Aircraft:
    """One aircraft as seen in a single poll."""

    meta:      AircraftMeta
    location:  AircraftLocation
    direction: AircraftVector
    route:     AircraftRoute
    airframe:  Airframe
    raw:       AircraftRaw


# ---------------------------------------------------------------------------
# Poll envelope
# ---------------------------------------------------------------------------

@dataclass
class SquawkEnvelope:
    """
    One object per poll cycle from one ingestor.
    The ingestor is responsible for merging data from multiple receivers
    before emitting this envelope. Downstream modules treat this as a single
    authoritative snapshot. Each aircraft's AircraftMeta.ingestor identifies
    the source; AircraftMeta.observed_at is the storage merge key.
    """

    source:          str                      # Ingestor identity, e.g. "PersonalADSB"
    timestamp:       datetime                 # UTC time this poll completed
    aircraft_count:  int                      # Convenience field — len(aircraft)

    receiver_status: list[ReceiverStatus] = field(default_factory=list)
    aircraft:        list[Aircraft]       = field(default_factory=list)


# ---------------------------------------------------------------------------
# Deserialiser
# ---------------------------------------------------------------------------

def aircraft_from_dict(d: dict) -> Aircraft:
    """Reconstruct an Aircraft from a dict produced by SquawkEncoder."""
    m   = d["meta"]
    loc = d["location"]
    vec = d["direction"]
    rt  = d["route"]
    af  = d["airframe"]
    oa  = m.get("observed_at")

    return Aircraft(
        meta=AircraftMeta(
            icao_hex       = m["icao_hex"],
            ingestor       = m["ingestor"],
            observed_at    = datetime.fromisoformat(oa) if oa else None,
            reception_type = m["reception_type"],
        ),
        location=AircraftLocation(
            latitude        = loc["latitude"],
            longitude       = loc["longitude"],
            altitude_feet   = loc["altitude_feet"],
            distance_nm     = loc["distance_nm"],
            bearing_degrees = loc["bearing_degrees"],
            seen_seconds    = loc["seen_seconds"],
        ),
        direction=AircraftVector(
            ground_speed_knots = vec["ground_speed_knots"],
            track_degrees      = vec["track_degrees"],
            vertical_rate_fpm  = vec["vertical_rate_fpm"],
        ),
        route=AircraftRoute(
            callsign         = rt["callsign"],
            squawk_code      = rt["squawk_code"],
            origin_iata      = rt["origin_iata"],
            destination_iata = rt["destination_iata"],
            flight_number    = rt["flight_number"],
        ),
        airframe=Airframe(
            registration  = af["registration"],
            aircraft_type = af["aircraft_type"],
            operator      = af["operator"],
        ),
        raw=AircraftRaw(payload=d["raw"]["payload"]),
    )
