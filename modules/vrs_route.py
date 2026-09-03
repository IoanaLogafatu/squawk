"""
modules/vrs_route.py

Enriches AircraftRoute from VRS standing-data (data_sources/vrs_standing_data.py)
— routes joined against airports and countries.

For each aircraft with a callsign and any route field still UNKNOWN, looks up
the callsign in the routes table. On a hit:

  - airport_codes splits on '-'; first and last are origin/destination — the
    two-leg case the wall displays. A multi-stop route (three or more codes)
    reduces to first/last; the middle stop is discarded, not modelled. A
    round trip (first == last, e.g. "EGAA-GCRR-EGAA") is a real positioning
    flight and resolves with origin equal to destination — not an error.
  - each of origin/destination is joined against the airports table for
    *_iata, *_name, *_municipality (airports.location — a city, e.g. "Fort
    Myers", not a repeat of the airport name), and *_country (airports
    .country_iso2 resolved through the countries table to a full name, e.g.
    "Spain" — matching the shape adsbdb already writes to this field, not a
    second convention storing the raw ISO code).
  - airline_name comes from airlines.name via routes.airline_code.

flight_number and airline_country are deliberately left UNKNOWN by this
module. Verified against a real VLG (Vueling) shard from the live repo
tonight: routes.Code is "VLG" (Vueling's *ICAO* code), not "VY" (its IATA
code) — Code is uniformly ICAO-style across the dataset, not the IATA-style
2-letter-prefix format AircraftRoute.flight_number's docstring expects
("BA117"). Writing an ICAO-shaped value into an IATA-shaped field would be
wrong just to fill it. airline_country has no source in VRS data at all (the
airlines table carries no country column). adsbdb — the only thing that
could currently fill either — is out of both processor chains for this
session (brief-vrs-route.md rev 2), so this is a real, visible gap while
that holds, not a hidden one: nothing downstream fills these two fields
right now.

Field writes are guarded exactly like adsbdb's _apply: `if
aircraft.route.origin_iata is None and ...`, so a value already present
survives. An airport code, country, or callsign with no matching row
degrades the same way missing data already does elsewhere — the field stays
UNKNOWN, nothing raises.

Unresolved logging: same shape as adsbdb's log_unresolved, unconditional
(no config toggle — with adsbdb out of the chain, this log is the only
visibility into what VRS-only route coverage is missing). One JSON line per
(hex, callsign) appended to <module_dir>/unresolved.jsonl:
    no_callsign      — the aircraft has not transmitted identity
    unknown_callsign — the routes table has no row for this callsign

Debug logging (console, separate from the unresolved log above) is gated by
`log_level`:
    "none"    — silent
    "errors"  (default) — a callsign that resolved to nothing prints one line
    "verbose" — every lookup prints a line, hit or miss

Negative-result cache: a miss (unknown_callsign or no_callsign) is
remembered for `_NOT_FOUND_TTL_SECONDS` (1 hour — same number and reasoning
as adsbdb's own `_ROUTE_TTL_SECONDS`: "a route is a property of today's
flight"). While a key is within its window, the aircraft is skipped
entirely before doing anything else — no get_route() call, no console
line at any log_level, no unresolved.jsonl append. Without this, a
callsign genuinely absent from VRS gets re-queried, re-printed and
re-logged every single cycle for as long as that hex stays in the pot,
which measures nothing beyond what one entry per TTL window already does
and just makes the log harder to read. Keyed by callsign for
unknown_callsign (a callsign is a property of one flight — if VRS doesn't
have it now, it won't gain it in the next 5 seconds) and by icao_hex for
no_callsign (not a VRS miss at all, just an aircraft that structurally
doesn't broadcast a callsign — GA/military at low altitude is the common
case). A key found in a subsequent hit (plausible after the weekly
vrs_standing_data refresh) is cleared rather than left stale alongside a
fresh positive result.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from modules import BaseModule, ModuleContext
from schemas.aircraft import Aircraft, AircraftRoute


_VALID_LOG_LEVELS = {"none", "errors", "verbose"}
_DEFAULT_LOG_LEVEL = "errors"

_UNRESOLVED_LOG = "unresolved.jsonl"

_NOT_FOUND_TTL_SECONDS = 3600   # 1 hour — same as adsbdb's _ROUTE_TTL_SECONDS


def _validate_log_level(value: object) -> str:
    """Return `value` as a recognised log level, or `_DEFAULT_LOG_LEVEL` if unset.

    Unlike tar1090_db's refresh_days, there's no numeric range to reject —
    only membership in the three literal strings — but the same
    rejection-not-silent-default shape applies: a typo'd level fails at
    startup rather than silently behaving as "errors".
    """
    if value is None:
        return _DEFAULT_LOG_LEVEL
    if value not in _VALID_LOG_LEVELS:
        raise ValueError(
            f"vrs_route: 'log_level' must be one of {sorted(_VALID_LOG_LEVELS)}, got {value!r}"
        )
    return value


def _needs_route(route: AircraftRoute) -> bool:
    """True if any field this module can fill is still UNKNOWN.

    flight_number and airline_country are deliberately excluded — this
    module never writes them (see module docstring), so including them here
    would make every aircraft look permanently incomplete and defeat the
    point of the guard.
    """
    return (
        route.origin_iata is None
        or route.origin_name is None
        or route.origin_municipality is None
        or route.origin_country is None
        or route.destination_iata is None
        or route.destination_name is None
        or route.destination_municipality is None
        or route.destination_country is None
        or route.airline_name is None
    )


class VrsRouteEnricher(BaseModule):

    def __init__(self, source, log_dir: Path, log_level: str = _DEFAULT_LOG_LEVEL) -> None:
        self._source = source
        self._log_dir = log_dir
        self._log_level = log_level

        # callsign (or "hex:<icao_hex>" for a no_callsign aircraft) -> the
        # time.time() this key was last confirmed missing. Module instances
        # are pooled and could in principle be shared across chains running
        # in separate threads, so this is lock-guarded even though today's
        # only wiring (ingest) is single-threaded — matching the convention
        # every other pooled instance in this codebase follows.
        self._not_found: dict[str, float] = {}
        self._not_found_lock = threading.Lock()

    def process(self, aircraft: list[Aircraft]) -> list[Aircraft]:
        # Not just at construction — a long-lived instance must keep noticing
        # a newer dataset. See docs/data-sources-guide.md.
        self._source.ensure_fresh()
        db = self._source.db
        if db is None:
            return aircraft

        self._sweep_not_found()

        for a in aircraft:
            hex_id = (a.meta.icao_hex or "").strip().upper() or None
            callsign = (a.route.callsign or "").strip().upper() or None

            # Before doing anything else: a key confirmed missing within the
            # last _NOT_FOUND_TTL_SECONDS is a complete skip — no DB call, no
            # console line at any log_level, no unresolved-log write. See
            # module docstring.
            key = callsign or f"hex:{hex_id or ''}"
            with self._not_found_lock:
                last = self._not_found.get(key)
            if last is not None and time.time() - last < _NOT_FOUND_TTL_SECONDS:
                continue

            if callsign is None:
                self._record_unresolved(hex_id, None, "no_callsign", a.airframe.registration)
                if self._log_level in ("errors", "verbose"):
                    print(f"  vrs_route: no callsign for {hex_id or '?'}")
                with self._not_found_lock:
                    self._not_found[key] = time.time()
                continue

            if not _needs_route(a.route):
                continue

            row = db.get_route(callsign)
            if row is None:
                self._record_unresolved(hex_id, callsign, "unknown_callsign", a.airframe.registration)
                if self._log_level in ("errors", "verbose"):
                    print(f"  vrs_route: callsign {callsign} unknown — no route")
                with self._not_found_lock:
                    self._not_found[key] = time.time()
                continue

            # A route that used to be missing has now appeared (plausible
            # after the weekly vrs_standing_data refresh) — don't leave a
            # stale negative entry sitting alongside the fresh positive result.
            with self._not_found_lock:
                self._not_found.pop(key, None)

            self._apply_route(a, row, db)

            if self._log_level == "verbose":
                origin = a.route.origin_iata or "?"
                dest   = a.route.destination_iata or "?"
                print(f"  vrs_route: callsign {callsign} returned {origin} - {dest}")

        return aircraft

    def _sweep_not_found(self) -> None:
        now = time.time()
        with self._not_found_lock:
            dead = [k for k, t in self._not_found.items() if now - t > _NOT_FOUND_TTL_SECONDS]
            for k in dead:
                del self._not_found[k]

    # -----------------------------------------------------------------
    # Apply
    # -----------------------------------------------------------------

    def _apply_route(self, aircraft: Aircraft, row: tuple, db) -> None:
        code, number, airline_code, airport_codes = row
        # code/number deliberately unused — see module docstring: routes.Code
        # is ICAO-style, not the IATA-style format flight_number expects.

        codes = [c for c in (airport_codes or "").split("-") if c]
        if len(codes) >= 2:
            origin_icao, destination_icao = codes[0], codes[-1]

            origin = db.get_airport(origin_icao)
            if origin is not None:
                if aircraft.route.origin_iata is None and origin.iata:
                    aircraft.route.origin_iata = origin.iata
                if aircraft.route.origin_name is None and origin.name:
                    aircraft.route.origin_name = origin.name
                if aircraft.route.origin_municipality is None and origin.location:
                    aircraft.route.origin_municipality = origin.location
                if aircraft.route.origin_country is None and origin.country_iso2:
                    country = db.get_country(origin.country_iso2)
                    if country is not None and country.name:
                        aircraft.route.origin_country = country.name

            destination = db.get_airport(destination_icao)
            if destination is not None:
                if aircraft.route.destination_iata is None and destination.iata:
                    aircraft.route.destination_iata = destination.iata
                if aircraft.route.destination_name is None and destination.name:
                    aircraft.route.destination_name = destination.name
                if aircraft.route.destination_municipality is None and destination.location:
                    aircraft.route.destination_municipality = destination.location
                if aircraft.route.destination_country is None and destination.country_iso2:
                    country = db.get_country(destination.country_iso2)
                    if country is not None and country.name:
                        aircraft.route.destination_country = country.name

        if airline_code and aircraft.route.airline_name is None:
            airline = db.get_airline(airline_code)
            if airline is not None and airline.name:
                aircraft.route.airline_name = airline.name

    # -----------------------------------------------------------------
    # Unresolved-route diagnostic log
    # -----------------------------------------------------------------

    def _record_unresolved(
        self, hex_id: str | None, callsign: str | None, reason: str, registration: str | None,
    ) -> None:
        """Append one line for a route this module could not resolve — see
        module docstring for why this has no config toggle. Called only on a
        genuine fresh miss or a TTL-expired retry; the not-found cache in
        process() is what keeps a still-missing key from writing a new line
        every cycle, so no dedup is needed here."""
        line = json.dumps({
            "at":           datetime.now(timezone.utc).isoformat(),
            "hex":          hex_id,
            "callsign":     callsign,
            "registration": registration,
            "reason":       reason,
        }, ensure_ascii=False)

        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            with open(self._log_dir / _UNRESOLVED_LOG, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as exc:
            print(f"  vrs_route: could not write unresolved log: {exc}")


KEYS = {"log_level"}


def get(cfg: dict, ctx: ModuleContext) -> VrsRouteEnricher:
    source = ctx.data_source(cfg["source"])
    log_level = _validate_log_level(cfg.get("log_level"))
    return VrsRouteEnricher(source=source, log_dir=ctx.module_dir, log_level=log_level)
