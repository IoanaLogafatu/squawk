"""
schemas/aircraft.py

Canonical data schema for the Squawk aircraft ingestor.

All fields are always present. Fields the source cannot populate are set to
UNKNOWN (None). Downstream modules use None as a signal that they should
attempt to fill the field.

Sections:
    AircraftMeta     — identity and provenance (ingestor, observed_at)
    AircraftLocation — where the aircraft is (position, altitude, range)
    AircraftVector   — how it is moving (speed, track, climb rate)
    AircraftRoute    — the flight it is operating (callsign, squawk, origin/dest)
    Airframe         — the physical aircraft (registration, type, operator)
    AircraftRaw      — full unmodified source payload
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


# Sentinel — explicit alias so intent is clear throughout the codebase
UNKNOWN = None


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
    altitude_band  — Flight-level band letter derived from altitude_feet, assigned
                     by the altitude_band module at ingest. Bands are lettered from
                     "A" upward against that installation's configured edges, so
                     the letter is meaningless without them — change the edges and
                     every letter changes meaning. Any human-readable form
                     ("FL200-FL300") is display copy and lives in panel config.
    distance_nm    — Distance from the receiver in nautical miles (readsb r_dst).
    bearing_degrees — Bearing from receiver to aircraft (readsb r_dir), 0–359.
    seen_seconds   — Seconds since any message was received from this aircraft.
    """

    latitude:        Optional[float] = UNKNOWN
    longitude:       Optional[float] = UNKNOWN
    altitude_feet:   Optional[int]   = UNKNOWN   # Barometric; 0 = on ground
    altitude_band:   Optional[str]   = UNKNOWN   # Flight-level band letter, e.g. "C"
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
    origin/dest    — Not in ADS-B — enrichment modules only.
    *_municipality — The city an airport serves, kept separate from *_name:
                     adsbdb gives both, and for CDG the name is "Charles de
                     Gaulle International Airport" where the municipality is
                     "Paris". A display generally wants the city; the full
                     airport name is legitimate data in its own right.
    flight_number  — Not in ADS-B — enrichment modules only.
    """

    callsign:            Optional[str] = UNKNOWN   # ICAO flight number / radio callsign
    squawk_code:         Optional[str] = UNKNOWN   # 4-digit octal transponder code
    origin_iata:         Optional[str] = UNKNOWN   # Departure airport, e.g. "LHR"
    origin_name:         Optional[str] = UNKNOWN   # e.g. "Reus Airport"
    origin_municipality: Optional[str] = UNKNOWN   # City served, e.g. "Reus"
    origin_country:      Optional[str] = UNKNOWN   # e.g. "Spain"
    destination_iata:    Optional[str] = UNKNOWN   # Arrival airport, e.g. "JFK"
    destination_name:    Optional[str] = UNKNOWN   # e.g. "Leeds Bradford Airport"
    destination_municipality: Optional[str] = UNKNOWN   # City served, e.g. "Leeds"
    destination_country: Optional[str] = UNKNOWN   # e.g. "United Kingdom"
    flight_number:       Optional[str] = UNKNOWN   # Commercial flight number, e.g. "BA117"
    airline_name:        Optional[str] = UNKNOWN   # e.g. "Ryanair"
    airline_country:     Optional[str] = UNKNOWN   # e.g. "Ireland"


@dataclass
class Airframe:
    """
    The physical aircraft. Long-lived data tied to the airframe (or its transponder).

    operator — Registered owner of the airframe (e.g. "Malta Air"). Separate from
               airline_name on AircraftRoute, which is who is flying it this flight.

    type_code vs type_description — the designator is machine-readable and stable
               across sources; the description is prose and varies by source
               ("AIRBUS A-320" / "Airbus A320-214"). Match on the code, display
               the description. They are separate fields because a single one
               with several writers ends up holding whichever shape answered last.

    category — ICAO ADS-B emitter category, straight off the transmission:
               A0 no information, A1 light (<15,500 lb), A2 small (15,500-75,000),
               A3 large (75,000-300,000), A4 B757, A5 heavy (>300,000),
               A6 high performance, A7 rotorcraft, B1 glider, B2 lighter-than-air,
               B4 ultralight, B6 UAV, B7 space vehicle, C0-C7 surface vehicles
               and obstacles.

               Operator-configured, so occasionally wrong; A0 is common; Mode S-only
               and MLAT tracks do not carry it at all. Treat absence as normal.

    db_flags — tar1090 bitfield: 1 military, 2 interesting, 4 PIA, 8 LADD.
               Stored as the raw integer rather than decoded booleans; that is
               what both sources supply and it round trips cleanly.

               `None` means "we don't know" and is NOT the same as 0 ("no flags
               set"). Only 0 is permission to treat an aircraft as unflagged —
               absence of information is not information.
    """

    registration:     Optional[str] = UNKNOWN   # Tail number, e.g. "G-EUPT"
    type_code:        Optional[str] = UNKNOWN   # ICAO designator, e.g. "A320", "B38M"
    type_description: Optional[str] = UNKNOWN   # Human-readable, e.g. "AIRBUS A-320"
    category:         Optional[str] = UNKNOWN   # ADS-B emitter category, e.g. "A3"
    db_flags:         Optional[int] = UNKNOWN   # tar1090 bitfield: 1 military, 2 interesting, 4 PIA, 8 LADD
    manufacturer:     Optional[str] = UNKNOWN   # e.g. "Boeing"
    operator:         Optional[str] = UNKNOWN   # Registered owner, e.g. "Malta Air"


@dataclass
class AircraftRaw:
    """
    Full unmodified payload from the source.
    Lets consumers or modules access fields not mapped into the Squawk schema —
    for example: wind data, integrity fields (NIC/NAC/SIL), nav modes, RSSI.
    """

    payload: dict[str, Any] = field(default_factory=dict)   # ingestor payload
    adsbdb:  dict[str, Any] = field(default_factory=dict)   # full adsbdb response


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
            # .get(): records written before the band existed stay readable and
            # acquire a letter on the aircraft's next observation.
            altitude_band   = loc.get("altitude_band"),
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
            callsign            = rt["callsign"],
            squawk_code         = rt["squawk_code"],
            origin_iata         = rt["origin_iata"],
            origin_name         = rt.get("origin_name"),
            origin_municipality = rt.get("origin_municipality"),
            origin_country      = rt.get("origin_country"),
            destination_iata    = rt["destination_iata"],
            destination_name    = rt.get("destination_name"),
            destination_municipality = rt.get("destination_municipality"),
            destination_country = rt.get("destination_country"),
            flight_number       = rt["flight_number"],
            airline_name        = rt.get("airline_name"),
            airline_country     = rt.get("airline_country"),
        ),
        airframe=Airframe(
            registration     = af["registration"],
            # .get() for the type/category fields: records written before they
            # existed are still readable, and simply acquire them on the
            # aircraft's next observation. No migration.
            type_code        = af.get("type_code"),
            type_description = af.get("type_description"),
            category         = af.get("category"),
            db_flags         = af.get("db_flags"),
            manufacturer     = af.get("manufacturer"),
            operator         = af["operator"],
        ),
        raw=AircraftRaw(
            payload = d["raw"]["payload"],
            adsbdb  = d["raw"].get("adsbdb", {}),
        ),
    )
