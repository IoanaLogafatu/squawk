"""
modules/registration_filter.py

Processor module that filters aircraft matching a configured list of target registrations.
Registration normally arrives from ingest-time enrichment, so this filter can sit anywhere
in a chain. If an installation runs tar1090_db in the chain instead, it must sit ahead of
this filter.
"""

from __future__ import annotations

from modules import BaseModule, ModuleContext
from schemas.aircraft import Aircraft


class RegistrationFilter(BaseModule):

    def __init__(self, target_registrations: list[str] | str) -> None:
        if isinstance(target_registrations, str):
            target_registrations = [target_registrations]
        self._target_registrations = set(
            r.strip().upper() for r in target_registrations if r and r.strip()
        )

    def process(self, aircraft: list[Aircraft]) -> list[Aircraft]:
        if not self._target_registrations:
            return []

        filtered = []
        for a in aircraft:
            if a.airframe.registration is None:
                continue
            reg = a.airframe.registration.strip().upper()
            if reg in self._target_registrations:
                filtered.append(a)

        return filtered


KEYS = {"registrations"}


def get(cfg: dict, ctx: ModuleContext) -> RegistrationFilter:
    target_registrations = cfg.get("registrations", [])
    return RegistrationFilter(target_registrations=target_registrations)

