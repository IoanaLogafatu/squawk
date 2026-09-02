"""
modules/altitude_band.py

Ingest-time enrichment that tags each aircraft with a flight-level band
letter derived from its barometric altitude.

The letter is the identity. Bands are lettered from "A" upward against the
installation's configured `edges`, so a letter means nothing without them —
change the edges and every letter changes meaning. Any human-readable form
("FL200-FL300") is display copy and belongs in panel config, not here and
not in the schema.

Reads location.altitude_feet and nothing else. That field is already
normalised barometric altitude with "ground" mapped to 0, which is exactly
what a flight level is measured against, so there is no conversion here and
no alt_geom fallback: a band derived from geometric altitude would not be a
flight level. An aircraft with no known altitude keeps an UNKNOWN band.

Assignment is half-open upward. With edges = [10000, 20000, 30000]:

    A  alt < 10000                 below FL100
    B  10000 <= alt < 20000        FL100-FL200
    C  20000 <= alt < 30000        FL200-FL300
    D  alt >= 30000                above FL300

Unlike tar1090_db this writes *derived* state into storage — every
data/tracked_aircraft/*.json carries a band letter. It cannot go stale
relative to the altitude beside it: both are rewritten together on every
ingest cycle.
"""

from __future__ import annotations

import bisect

from modules import BaseModule, ModuleContext
from schemas.aircraft import Aircraft, UNKNOWN


# 25 edges give 26 bands, A-Z — one more and the letters stop being single
# characters, which is the whole shape of the identifier.
_MAX_EDGES = 25


def _validate_edges(edges: object) -> list[float]:
    """Return the edges as a list, or raise ValueError explaining why not."""
    if edges is None:
        raise ValueError(
            "altitude_band: 'edges' is required — set it to an ascending list of "
            "flight-level boundaries in feet, e.g. edges = [10000, 20000, 30000]"
        )
    if not isinstance(edges, (list, tuple)):
        raise ValueError(f"altitude_band: 'edges' must be a list, got {type(edges).__name__}")
    if not edges:
        raise ValueError("altitude_band: 'edges' is empty — a band scheme needs at least one edge")
    if len(edges) > _MAX_EDGES:
        raise ValueError(
            f"altitude_band: {len(edges)} edges is more than the maximum of {_MAX_EDGES} — "
            "band letters are single characters, so at most 26 bands can be named"
        )

    for edge in edges:
        # bool is an int subclass; True as an altitude is a config mistake.
        if isinstance(edge, bool) or not isinstance(edge, (int, float)):
            raise ValueError(f"altitude_band: edge {edge!r} is not a number")
        if edge <= 0:
            raise ValueError(f"altitude_band: edge {edge!r} must be positive")
        if edge % 100 != 0:
            raise ValueError(
                f"altitude_band: edge {edge!r} is not a multiple of 100 — every boundary "
                "must be a real flight level, or a derived label reads as FL125.5"
            )

    for lower, upper in zip(edges, edges[1:]):
        if upper <= lower:
            raise ValueError(
                f"altitude_band: 'edges' must be strictly ascending — {upper!r} follows {lower!r}"
            )

    return list(edges)


class AltitudeBand(BaseModule):

    def __init__(self, edges: object = None) -> None:
        self._edges = _validate_edges(edges)
        # N edges give N+1 bands, lettered from "A".
        self._letters = [chr(ord("A") + i) for i in range(len(self._edges) + 1)]

    def process(self, aircraft: list[Aircraft]) -> list[Aircraft]:
        for a in aircraft:
            altitude = a.location.altitude_feet
            if altitude is None:
                a.location.altitude_band = UNKNOWN
                continue
            # bisect_right, not bisect_left: an altitude sitting exactly on an
            # edge belongs to the band above it.
            a.location.altitude_band = self._letters[bisect.bisect_right(self._edges, altitude)]
        return aircraft


KEYS = {"edges"}


def get(cfg: dict, ctx: ModuleContext) -> AltitudeBand:
    return AltitudeBand(edges=cfg.get("edges"))
