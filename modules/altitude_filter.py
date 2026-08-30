"""
modules/altitude_filter.py

Processor filter module that filters aircraft based on altitude bounds (above and below),
supporting selection of altitude source (alt_baro or alt_geom) and fallback behavior.
"""

from __future__ import annotations

from typing import Any, Optional
from modules import BaseModule
from schemas.aircraft import Aircraft


class AltitudeFilter(BaseModule):

    def __init__(
        self,
        above: Optional[float] = None,
        below: Optional[float] = None,
        altitude_source: str = "alt_baro",
        fallback: bool = True,
    ) -> None:
        if altitude_source not in ("alt_baro", "alt_geom"):
            raise ValueError(f"altitude_filter: invalid altitude_source {altitude_source!r}. Must be 'alt_baro' or 'alt_geom'")

        self._above = float(above) if above is not None else None
        self._below = float(below) if below is not None else None
        self._altitude_source = altitude_source
        self._fallback = bool(fallback)

        if self._above is not None and self._below is not None and self._above > self._below:
            raise ValueError(
                f"altitude_filter: 'above' ({above}) cannot be greater than 'below' ({below})"
            )

    def _parse_alt(self, val: Any) -> Optional[float]:
        if val is None:
            return None
        if val == "ground":
            return 0.0
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    def _resolve_altitude(self, a: Aircraft) -> Optional[float]:
        alt_baro = None
        if a.location.altitude_feet is not None:
            alt_baro = float(a.location.altitude_feet)
        else:
            alt_baro = self._parse_alt(a.raw.payload.get("alt_baro"))

        alt_geom = self._parse_alt(a.raw.payload.get("alt_geom"))

        if self._altitude_source == "alt_baro":
            primary, secondary = alt_baro, alt_geom
        else:
            primary, secondary = alt_geom, alt_baro

        if primary is not None:
            return primary
        if self._fallback and secondary is not None:
            return secondary
        return None

    def process(self, aircraft: list[Aircraft]) -> list[Aircraft]:
        filtered = []
        for a in aircraft:
            alt = self._resolve_altitude(a)
            if alt is None:
                continue
            if self._above is not None and alt < self._above:
                continue
            if self._below is not None and alt > self._below:
                continue
            filtered.append(a)
        return filtered


KEYS = {"type", "above", "below", "altitude_source", "fallback"}


def get(cfg: dict) -> AltitudeFilter:
    above = cfg.get("above")
    below = cfg.get("below")
    altitude_source = cfg.get("altitude_source", "alt_baro")
    fallback = cfg.get("fallback", True)
    return AltitudeFilter(
        above=above,
        below=below,
        altitude_source=altitude_source,
        fallback=fallback,
    )
