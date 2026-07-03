"""
modules/pass_through.py

Pass-through module. Returns the aircraft list unchanged.
Useful as a placeholder slot in the pipeline during development,
or to reserve a position for a future filter.
"""

from __future__ import annotations

from modules import BaseModule
from schemas.aircraft import Aircraft


class PassThrough(BaseModule):

    def process(self, aircraft: list[Aircraft]) -> list[Aircraft]:
        return aircraft


def get(cfg: dict) -> PassThrough:
    return PassThrough()
