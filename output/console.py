"""
output/console.py

Prints the current picture to stdout, one line per aircraft.

The format is fixed-width so a stream of polls reads as a table rather
than as ragged text, and it leads with the fields that change between
polls — position, altitude, range — because watching those move is how
you tell the pipeline is alive.

Each poll is flushed as it is written. Python block-buffers stdout when
it is not a terminal, so without this a chain piped to a file or a log
collector appears to produce nothing at all for minutes at a time.
"""

from __future__ import annotations

import sys

from schemas.aircraft import Aircraft, now_utc
from output.base import BaseOutput

HEADER = (
    f"{'HEX':<8} {'CALLSIGN':<10} {'REG':<8} {'TYPE':<5} "
    f"{'LAT':>10} {'LON':>11} {'ALT':>7} {'SPD':>6} {'TRK':>5} {'V/S':>7} {'DIST':>7}  ROUTE"
)


class ConsoleOutput(BaseOutput):

    def send(self, aircraft: list[Aircraft]) -> None:
        # Marked Z: log lines carry local time, so an unlabelled UTC
        # stamp next to them reads as a clock that is running wrong.
        stamp = now_utc().strftime("%H:%M:%SZ")
        print(f"\n[{stamp}] {self.chain.name}: {len(aircraft)} aircraft")

        if not aircraft:
            print("  (nothing in the snapshot)")
            sys.stdout.flush()
            return

        print(f"  {HEADER}")
        for one in aircraft:
            print(f"  {_format(one)}")
        sys.stdout.flush()

    def on_deleted(self, aircraft: list[Aircraft]) -> None:
        for one in aircraft:
            print(f"  GONE  {one.meta.icao_hex or '??????'} "
                  f"{one.route.callsign or '':<10} last seen {one.meta.last_seen}")
        sys.stdout.flush()


def _format(one: Aircraft) -> str:
    loc   = one.location
    dirn  = one.direction
    route = one.route

    origin      = route.origin.iata or route.origin.icao or "???"
    destination = route.destination.iata or route.destination.icao or "???"

    return (
        f"{one.meta.icao_hex or '??????':<8} "
        f"{route.callsign or '-':<10} "
        f"{one.airframe.registration or '-':<8} "
        f"{one.airframe.type_code or '-':<5} "
        f"{_num(loc.latitude, 4):>10} "
        f"{_num(loc.longitude, 4):>11} "
        f"{_num(loc.altitude_feet, 0):>7} "
        f"{_num(dirn.ground_speed_knots, 0):>6} "
        f"{_num(dirn.track_degrees, 0):>5} "
        f"{_num(dirn.vertical_rate_fpm, 0):>7} "
        f"{_num(loc.distance_nm, 1):>7}  "
        f"{origin}->{destination}"
    )


def _num(value: float | int | None, places: int) -> str:
    """UNKNOWN prints as a dash — an empty column would read as zero."""
    if value is None:
        return "-"
    return f"{value:.{places}f}"
