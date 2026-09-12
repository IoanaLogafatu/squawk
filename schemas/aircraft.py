"""
schemas/aircraft.py

Canonical data schema for Squawk.

Every field is optional and defaults to UNKNOWN (None). Ingest sources
populate the picture incrementally, and the snapshot's "only update if
blank" merge rule needs to tell "not yet known" apart from "known to be
zero / empty" — so None is the single sentinel for the former, everywhere.

Sub-objects (Location, Route, Airport, ...) are always present on the
aircraft; it is their leaf fields that are optional. That keeps the merge
logic a straight recursive walk with no "does this section exist yet?"
special cases.

Sections:
    Meta      — identity and provenance (icao_hex, which chain saw it, last_seen)
    Location  — where the aircraft is (position, altitude, range)
    Direction — how it is moving (speed, track, climb rate)
    Route     — the flight being operated (callsign, origin, destination, airline)
    Airframe  — the physical aircraft (registration, type, operator)
    raw       — unmodified source payloads, keyed by whoever produced them

Aircraft always travel between modules as list[Aircraft], even where a
source only ever produces one, so every module has the same shaped input.
"""

from __future__ import annotations

import json
import types
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Union, get_args, get_origin, get_type_hints


# Sentinel — explicit alias so intent is clear throughout the codebase
UNKNOWN = None


# ---------------------------------------------------------------------------
# Shared sub-objects
# ---------------------------------------------------------------------------

@dataclass
class Airport:
    """
    One end of a route.

    Both an IATA and an ICAO code are kept side by side: enrichment sources
    differ on which they speak natively, and an airport with no IATA code
    (military, GA) still has an ICAO one — a single field with several
    writers would just hold whichever shape answered last.

    municipality is the city served, kept apart from name: for CDG the name
    is "Charles de Gaulle International Airport" and the municipality is
    "Paris". Displays generally want the city; the full name is data in its
    own right.
    """

    iata:         Optional[str] = UNKNOWN   # e.g. "LHR"
    icao:         Optional[str] = UNKNOWN   # e.g. "EGLL"
    name:         Optional[str] = UNKNOWN   # e.g. "London Heathrow Airport"
    municipality: Optional[str] = UNKNOWN   # City served, e.g. "London"
    country:      Optional[str] = UNKNOWN   # e.g. "United Kingdom"


@dataclass
class Airline:
    """The airline operating the flight. Same IATA/ICAO split as Airport."""

    name:    Optional[str] = UNKNOWN   # e.g. "British Airways"
    iata:    Optional[str] = UNKNOWN   # e.g. "BA"
    icao:    Optional[str] = UNKNOWN   # e.g. "BAW"
    country: Optional[str] = UNKNOWN   # e.g. "United Kingdom"


@dataclass
class Airframe:
    """
    The physical aircraft. Long-lived data tied to the airframe.

    operator is the registered owner, which is not always the airline flying
    it on this particular flight — hence both this and Route.airline.

    type_code vs type_description: the designator is machine-readable and
    stable across sources; the description is prose and varies by source
    ("AIRBUS A-320" / "Airbus A320-214"). Match on the code, show the
    description.

    category is the ICAO ADS-B emitter category straight off the
    transmission (A1 light ... A5 heavy, B1 glider, C0-C7 surface). It is
    operator-configured, so occasionally wrong, and absent entirely from
    Mode S-only and MLAT tracks. Treat absence as normal.

    db_flags is the tar1090 bitfield: 1 military, 2 interesting, 4 PIA,
    8 LADD. None means "we don't know" and is NOT the same as 0, "no flags
    set" — only 0 is permission to treat an aircraft as unflagged.
    """

    registration:     Optional[str] = UNKNOWN   # Tail number, e.g. "G-BOAC"
    type_code:        Optional[str] = UNKNOWN   # ICAO designator, e.g. "CONC"
    type_description: Optional[str] = UNKNOWN   # Human-readable, e.g. "Concorde"
    category:         Optional[str] = UNKNOWN   # ADS-B emitter category, e.g. "A5"
    db_flags:         Optional[int] = UNKNOWN   # tar1090 bitfield
    manufacturer:     Optional[str] = UNKNOWN   # e.g. "BAC / Aerospatiale"
    operator:         Optional[str] = UNKNOWN   # Registered owner


# ---------------------------------------------------------------------------
# Per-aircraft sections
# ---------------------------------------------------------------------------

@dataclass
class Meta:
    """
    Identity and provenance. Set by the ingest chain, not the source.

    icao_hex       — 24-bit ICAO address, globally unique per transponder,
                     and the snapshot's primary key: it is the filename
                     every writer merges into. Assigned to the transponder
                     box rather than the airframe, so it moves if the
                     transponder is swapped.
    ingest_source  — Which ingest chain first saw this aircraft, e.g.
                     "concorde_A". It is a fill-if-blank field like the
                     rest, so a later chain merging into the record does
                     not take the credit — this is provenance for the
                     track, not for the most recent write.
    last_seen      — UTC time this observation was taken. The snapshot's
                     merge key: an incoming record is only merged if its
                     last_seen is newer than the one already on disk, and
                     the expiry loop deletes records whose last_seen has
                     fallen further behind than expiry_minutes.
    reception_type — How the data was obtained, e.g. "adsb_icao", "mlat".
    """

    icao_hex:       Optional[str]      = UNKNOWN   # e.g. "400F6A"
    ingest_source:  Optional[str]      = UNKNOWN   # e.g. "concorde_A"
    last_seen:      Optional[datetime] = UNKNOWN   # UTC; snapshot merge + expiry key
    reception_type: Optional[str]      = UNKNOWN   # e.g. "adsb_icao"


@dataclass
class Location:
    """
    Where the aircraft is right now.

    Overwritten wholesale by the snapshot merge — a position is only ever
    meaningful as a complete, self-consistent set, so filling blanks in it
    from an older record would invent an aircraft that was never there.
    """

    latitude:        Optional[float] = UNKNOWN
    longitude:       Optional[float] = UNKNOWN
    altitude_feet:   Optional[int]   = UNKNOWN   # Barometric; 0 = on ground
    altitude_band:   Optional[str]   = UNKNOWN   # Band letter, assigned by a transform
    distance_nm:     Optional[float] = UNKNOWN   # From the observer
    bearing_degrees: Optional[float] = UNKNOWN   # From the observer, 0-359
    seen_seconds:    Optional[float] = UNKNOWN   # Seconds since the last message


@dataclass
class Direction:
    """How the aircraft is moving. Overwritten wholesale, as Location is."""

    ground_speed_knots: Optional[float] = UNKNOWN
    track_degrees:      Optional[float] = UNKNOWN   # 0-359, true north
    vertical_rate_fpm:  Optional[int]   = UNKNOWN   # +ve climbing, -ve descending


@dataclass
class Route:
    """
    The flight being operated. Stable while the aircraft is in range.

    Only callsign and squawk_code come off the air; everything else here is
    filled in by enrichment transforms, which is exactly the case the
    fill-if-blank merge rule exists to serve.
    """

    callsign:      Optional[str] = UNKNOWN   # ICAO flight number / radio callsign
    squawk_code:   Optional[str] = UNKNOWN   # 4-digit octal transponder code
    flight_number: Optional[str] = UNKNOWN   # Commercial flight number, e.g. "BA117"
    origin:        Airport = field(default_factory=Airport)
    destination:   Airport = field(default_factory=Airport)
    airline:       Airline = field(default_factory=Airline)


# ---------------------------------------------------------------------------
# Per-aircraft record
# ---------------------------------------------------------------------------

@dataclass
class Aircraft:
    """
    One aircraft as seen in a single observation.

    raw is the safety net: unmapped source fields are kept here, keyed by
    whichever module produced them (e.g. {"concorde": {...}}), so nothing a
    source sent is ever discarded just because the schema has no home for
    it. Like Location and Direction it is overwritten wholesale on merge.
    """

    meta:      Meta      = field(default_factory=Meta)
    location:  Location  = field(default_factory=Location)
    direction: Direction = field(default_factory=Direction)
    route:     Route     = field(default_factory=Route)
    airframe:  Airframe  = field(default_factory=Airframe)
    raw:       dict[str, Any] = field(default_factory=dict)

    # -- serialisation ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Plain JSON-safe dict — datetimes become ISO 8601 strings."""
        return _to_plain(self)

    def to_json(self, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Aircraft":
        return _from_plain(cls, data)

    @classmethod
    def from_json(cls, text: str) -> "Aircraft":
        return cls.from_dict(json.loads(text))


# ---------------------------------------------------------------------------
# Generic dataclass <-> JSON conversion
#
# Written against the dataclass definitions rather than field-by-field, so
# adding a field to the schema above needs no matching edit down here — and
# so a snapshot file written before a field existed still loads, with that
# field simply left UNKNOWN.
# ---------------------------------------------------------------------------

def _to_plain(value: Any) -> Any:
    if is_dataclass(value):
        return {f.name: _to_plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_plain(v) for v in value]
    return value


def _unwrap_optional(hint: Any) -> Any:
    """Optional[X] / X | None -> X. Anything else is returned unchanged."""
    origin = get_origin(hint)
    # Union covers typing.Optional; types.UnionType covers the `X | None` form.
    if origin is Union or origin is types.UnionType:
        args = [a for a in get_args(hint) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return hint


def _coerce(hint: Any, raw: Any) -> Any:
    if raw is None:
        return None
    hint = _unwrap_optional(hint)
    if is_dataclass(hint):
        return _from_plain(hint, raw)
    if hint is datetime:
        return _parse_datetime(raw)
    return raw


def _from_plain(cls: Any, data: dict[str, Any]) -> Any:
    if not isinstance(data, dict):
        raise ValueError(f"expected an object for {cls.__name__}, got {type(data).__name__}")
    hints = get_type_hints(cls)
    kwargs = {}
    for f in fields(cls):
        # Absent keys fall through to the field default (UNKNOWN), which is
        # what makes older snapshot files readable after a schema addition.
        if f.name in data:
            kwargs[f.name] = _coerce(hints[f.name], data[f.name])
    return cls(**kwargs)


def _parse_datetime(raw: Any) -> datetime:
    if isinstance(raw, datetime):
        return ensure_utc(raw)
    return ensure_utc(datetime.fromisoformat(raw))


def ensure_utc(when: datetime) -> datetime:
    """
    Force a datetime to be timezone-aware UTC.

    Everything Squawk writes is aware UTC, but a hand-edited or
    externally-produced snapshot file could carry a naive timestamp, and
    comparing naive against aware raises. Assume naive means UTC rather
    than letting the merge blow up on someone's test fixture.
    """
    if when.tzinfo is None:
        return when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


def now_utc() -> datetime:
    """One place to ask the time, so tests have one place to patch it."""
    return datetime.now(timezone.utc)
