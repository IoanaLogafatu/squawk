"""
transforms/nop.py

The transform that does nothing.

It exists to prove the transform interface is genuinely wired into all
three chain families — ingest, snapshot and output all configure
transform = ["nop"] in the base build, so if any of those call sites is
broken it shows up here rather than on the first real transform.
"""

from __future__ import annotations

from schemas.aircraft import Aircraft
from transforms.base import BaseTransform


class Nop(BaseTransform):

    def process(self, aircraft: list[Aircraft]) -> list[Aircraft]:
        self.log.debug("nop: passing through %d aircraft unchanged", len(aircraft))
        return aircraft
