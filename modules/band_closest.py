"""
modules/band_closest.py

Selector that reduces the list to the nearest aircraft in each altitude
band — one per band, ordered highest band to lowest.

Groups on location.altitude_band, which the altitude_band enricher assigns
at ingest. This module holds no altitude logic and no thresholds of its
own: it groups on the letter it is given. An aircraft with no band, or no
distance to compare, is not a candidate — the same rule closest_filter
applies to distance.

Bands with no qualifying aircraft contribute nothing, so the result is
between zero and one entry per band and position in the list says nothing
about which band an entry came from. Consumers must read
location.altitude_band on each returned aircraft. That is what the tag is
for.

Run this before adsbdb: it cuts the list to at most one aircraft per band,
so the enricher performs a handful of route lookups per cycle rather than
one per aircraft in range.
"""

from __future__ import annotations

from modules import BaseModule, ModuleContext
from schemas.aircraft import Aircraft


class BandClosest(BaseModule):

    def process(self, aircraft: list[Aircraft]) -> list[Aircraft]:
        nearest: dict[str, Aircraft] = {}
        for a in aircraft:
            band     = a.location.altitude_band
            distance = a.location.distance_nm
            if band is None or distance is None:
                continue
            incumbent = nearest.get(band)
            # Strict <, so a tie leaves the incumbent in place: one aircraft
            # per band, whichever arrived first.
            if incumbent is None or distance < incumbent.location.distance_nm:
                nearest[band] = a
        # Descending by letter — the panel reads sky to ground.
        return [nearest[band] for band in sorted(nearest, reverse=True)]


KEYS: set[str] = set()   # no options of its own


def get(cfg: dict, ctx: ModuleContext) -> BandClosest:
    return BandClosest()
