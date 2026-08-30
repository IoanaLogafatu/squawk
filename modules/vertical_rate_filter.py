"""
modules/vertical_rate_filter.py

Processor filter module that filters aircraft based on vertical rate (climb / descent rate in feet per minute).
Supports 'min_fpm', 'max_fpm', or mode ('climbing', 'descending', 'level').
"""

from __future__ import annotations

from typing import Optional
from modules import BaseModule
from schemas.aircraft import Aircraft


class VerticalRateFilter(BaseModule):

    def __init__(
        self,
        min_fpm: Optional[float] = None,
        max_fpm: Optional[float] = None,
        mode: Optional[str] = None,
        threshold: float = 200.0,
    ) -> None:
        self._threshold = float(threshold)
        self._mode = mode.lower() if mode else None

        if self._mode == "climbing":
            self._min_fpm = self._threshold
            self._max_fpm = None
        elif self._mode == "descending":
            self._min_fpm = None
            self._max_fpm = -self._threshold
        elif self._mode == "level":
            self._min_fpm = -self._threshold
            self._max_fpm = self._threshold
        else:
            self._min_fpm = float(min_fpm) if min_fpm is not None else None
            self._max_fpm = float(max_fpm) if max_fpm is not None else None

    def process(self, aircraft: list[Aircraft]) -> list[Aircraft]:
        filtered = []
        for a in aircraft:
            vr = a.direction.vertical_rate_fpm
            if vr is None:
                continue
            if self._min_fpm is not None and vr < self._min_fpm:
                continue
            if self._max_fpm is not None and vr > self._max_fpm:
                continue
            filtered.append(a)
        return filtered


KEYS = {"type", "min_fpm", "max_fpm", "mode", "threshold"}


def get(cfg: dict) -> VerticalRateFilter:
    return VerticalRateFilter(
        min_fpm=cfg.get("min_fpm"),
        max_fpm=cfg.get("max_fpm"),
        mode=cfg.get("mode"),
        threshold=cfg.get("threshold", 200.0),
    )
