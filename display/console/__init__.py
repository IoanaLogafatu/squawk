"""
display/console/__init__.py

Display plugin. Logs the first aircraft in the list to stdout as
formatted JSON. Returns the list unchanged so further plugins could
be chained after it if needed.
"""

from __future__ import annotations

from plugins import BasePlugin
from schemas.aircraft import Aircraft


class ConsoleDisplay(BasePlugin):

    def process(self, aircraft: list[Aircraft]) -> list[Aircraft]:
        if not aircraft:
            print("  ○ no aircraft")
        else:
            a   = aircraft[0]
            reg  = a.airframe.registration  or "???"
            typ  = a.airframe.aircraft_type or "???"
            print(f"{reg}  {typ}")
        return aircraft


def get(cfg: dict) -> ConsoleDisplay:
    return ConsoleDisplay()
