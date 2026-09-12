"""
schemas/aircraft.py

Canonical data schema for Squawk, as laid down in the spec.

Every field is optional and defaults to UNKNOWN (None). Ingest sources
populate the picture incrementally, and the snapshot's merge rule skips an
incoming value that is blank rather than letting it erase what is held — so
it needs to tell "not yet known" apart from "known to be zero / empty", and
None is the single sentinel for the former, everywhere.

Sub-objects (Location, Route, Airport, ...) are always present on the
aircraft; it is their leaf fields that are optional. That keeps the merge
logic a straight recursive walk with no "does this section exist yet?"
special cases.

Sections:
    Meta      — identity and timing (icao_hex, squawk, first_seen, last_seen)
    Location  — where the aircraft is (latitude, longitude, altitude)
    Direction — how it is moving (speed, heading, climb rate)
    Route     — the flight being operated (callsign, flight number, origin, destination)
    Airframe  — the physical aircraft (registration, type, operator)
    Airline   — the airline operating the flight
    raw       — unmodified source payloads, one key per contributing module

Anything a source knows that has no field here — range from the observer,
signal strength, an emitter category — belongs in that source's raw entry,
not in a new field.

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
    (military, GA) still has an ICAO one.

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


# ---------------------------------------------------------------------------
# Per-aircraft sections
# ---------------------------------------------------------------------------

@dataclass
class Airline:
    """The airline operating the flight. Same IATA/ICAO split as Airport."""

    airline_name:    Optional[str] = UNKNOWN   # e.g. "British Airways"
    airline_iata:    Optional[str] = UNKNOWN   # e.g. "BA"
    airline_icao:    Optional[str] = UNKNOWN   # e.g. "BAW"
    airline_country: Optional[str] = UNKNOWN   # e.g. "United Kingdom"


@dataclass
class Airframe:
    """
    The physical aircraft. Long-lived data tied to the airframe.

    operator is the registered owner, which is not always the airline flying
    it on this particular flight — hence both this and Aircraft.airline.

    type_code vs type_description: the designator is machine-readable and
    stable across sources; the description is prose and varies by source
    ("AIRBUS A-320" / "Airbus A320-214"). Match on the code, show the
    description.
    """

    registration:     Optional[str] = UNKNOWN   # Tail number, e.g. "G-BOAC"
    type_code:        Optional[str] = UNKNOWN   # ICAO designator, e.g. "CONC"
    type_description: Optional[str] = UNKNOWN   # Human-readable, e.g. "Concorde"
    manufacturer:     Optional[str] = UNKNOWN   # e.g. "BAC / Aerospatiale"
    operator:         Optional[str] = UNKNOWN   # Registered owner


@dataclass
class Meta:
    """
    Identity and timing.

    icao_hex   — 24-bit ICAO address, globally unique per transponder, and
                 the snapshot's primary key: it is the filename every writer
                 merges into. Assigned to the transponder box rather than
                 the airframe, so it moves if the transponder is swapped.
    squawk     — 4-digit octal transponder code, e.g. "7700".
    first_seen — UTC time of the earliest observation of this aircraft in
                 the current snapshot record. The snapshot keeps the
                 earliest it is told of, and stamps it from last_seen when
                 a record is created without one.
    last_seen  — UTC time this observation was taken. It gates the
                 position fields in the snapshot merge — location and
                 direction are only taken from a record whose last_seen is
                 newer than the one held — and the expiry loop deletes
                 records whose last_seen has fallen further behind than
                 expiry_minutes.

    There is deliberately no "which source reported this" field. raw holds
    one key per contributing module, which already says who has reported,
    and a single last-writer field would only ever say who wrote last.
    """

    icao_hex:   Optional[str]      = UNKNOWN   # e.g. "400F6A"
    squawk:     Optional[str]      = UNKNOWN   # e.g. "2346"
    first_seen: Optional[datetime] = UNKNOWN   # UTC; earliest observation held
    last_seen:  Optional[datetime] = UNKNOWN   # UTC; position gate + expiry key


@dataclass
class Location:
    """
    Where the aircraft is right now.

    Replaced wholesale by the snapshot merge, and only by a newer
    observation — a position is only ever meaningful as a complete,
    self-consistent set, so filling blanks in it from an older record would
    invent an aircraft that was never there.
    """

    latitude:      Optional[float] = UNKNOWN
    longitude:     Optional[float] = UNKNOWN
    altitude_feet: Optional[float] = UNKNOWN   # Barometric; 0 = on ground


@dataclass
class Direction:
    """How the aircraft is moving. Replaced wholesale, as Location is."""

    ground_speed_knots: Optional[float] = UNKNOWN
    heading:            Optional[float] = UNKNOWN   # 0-359, true north
    vertical_rate_fpm:  Optional[float] = UNKNOWN   # +ve climbing, -ve descending


@dataclass
class Route:
    """
    The flight being operated. Stable while the aircraft is in range.

    Only the callsign comes off the air; everything else here is filled in
    by enrichment transforms. The merge takes any value an incoming record
    actually has and ignores its blanks, so one source can add to what
    another reported without erasing it.
    """

    callsign:      Optional[str] = UNKNOWN   # ICAO flight number / radio callsign
    flight_number: Optional[str] = UNKNOWN   # Commercial flight number, e.g. "BA117"
    origin:        Airport = field(default_factory=Airport)
    destination:   Airport = field(default_factory=Airport)


# ---------------------------------------------------------------------------
# Per-aircraft record
# ---------------------------------------------------------------------------

@dataclass
class Aircraft:
    """
    One aircraft as seen in a single observation.

    raw is the safety net: unmapped source fields are kept here, keyed by
    the config name of the module that produced them (e.g.
    {"concorde_A": {...}, "concorde_B": {...}}), so nothing a source sent is
    ever discarded just because the schema has no home for it. The merge
    replaces only the incoming module's own key, so several sources
    reporting on one aircraft never erase each other's payloads.
    """

    meta:      Meta      = field(default_factory=Meta)
    location:  Location  = field(default_factory=Location)
    direction: Direction = field(default_factory=Direction)
    route:     Route     = field(default_factory=Route)
    airframe:  Airframe  = field(default_factory=Airframe)
    airline:   Airline   = field(default_factory=Airline)
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
