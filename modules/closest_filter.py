"""
processor/modules/closest_filter.py

Reduces the aircraft list to a single entry: the one closest to the
receiver (lowest distance_nm). Aircraft without a known distance are
excluded as candidates. Returns an empty list if none qualify.
"""

from __future__ import annotations

from modules import BaseModule, ModuleContext
from schemas.aircraft import Aircraft


class ClosestFilter(BaseModule):

    def process(self, aircraft: list[Aircraft]) -> list[Aircraft]:
        candidates = [a for a in aircraft if a.location.distance_nm is not None]
        if not candidates:
            return []
        return [min(candidates, key=lambda a: a.location.distance_nm)]


KEYS: set[str] = set()   # no options of its own


def get(cfg: dict, ctx: ModuleContext) -> ClosestFilter:
    return ClosestFilter()
