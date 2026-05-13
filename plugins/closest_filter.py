"""
processor/plugins/closest_filter.py

Reduces the aircraft list to a single entry: the one closest to the
receiver (lowest distance_nm). Aircraft without a known distance are
excluded as candidates. Returns an empty list if none qualify.
"""

from __future__ import annotations

from plugins import BasePlugin
from schemas.aircraft import Aircraft


class ClosestFilter(BasePlugin):

    def process(self, aircraft: list[Aircraft]) -> list[Aircraft]:
        candidates = [a for a in aircraft if a.location.distance_nm is not None]
        if not candidates:
            return []
        return [min(candidates, key=lambda a: a.location.distance_nm)]


def get(cfg: dict) -> ClosestFilter:
    return ClosestFilter()
