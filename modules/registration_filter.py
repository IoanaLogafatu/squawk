"""
modules/registration_filter.py

Processor module that filters aircraft matching a configured list of target registrations,
resolving them to hex codes via the tar1090 database (aircraft.csv).
"""

from __future__ import annotations

import csv
from pathlib import Path

from modules import BaseModule
from schemas.aircraft import Aircraft


class RegistrationFilter(BaseModule):

    def __init__(self, target_registrations: list[str], data_dir: Path) -> None:
        if isinstance(target_registrations, str):
            target_registrations = [target_registrations]
        self._target_registrations = set(r.strip().upper() for r in target_registrations if r and r.strip())
        self._data_dir = data_dir
        self._csv_path = data_dir / "modules" / "tar1090_db" / "aircraft.csv"
        self._reg_to_hex: dict[str, str] = {}
        self._hex_to_reg: dict[str, str] = {}
        self._hex_to_type: dict[str, str] = {}
        self._target_hexes: set[str] = set()

        # Resolve tar1090_db download/existence
        if not self._csv_path.exists():
            from modules.tar1090_db import _download
            try:
                _download(self._csv_path)
            except Exception as e:
                print(f"registration_filter: failed to download tar1090_db database: {e}")

        self._load_aircraft_db()

    def _load_aircraft_db(self) -> None:
        if not self._csv_path.exists():
            return
        try:
            with open(self._csv_path, newline="", encoding="utf-8") as f:
                reader = csv.reader(f, delimiter=";")
                for row in reader:
                    if len(row) < 2:
                        continue
                    hex_code = row[0].strip().upper()
                    reg = row[1].strip().upper()
                    desc = (row[4].strip() if len(row) > 4 else "") or (row[2].strip() if len(row) > 2 else "")
                    if reg and hex_code:
                        self._reg_to_hex[reg] = hex_code
                        self._hex_to_reg[hex_code] = reg
                        if reg in self._target_registrations:
                            self._target_hexes.add(hex_code)
                    if hex_code and desc:
                        self._hex_to_type[hex_code] = desc
        except Exception as e:
            print(f"registration_filter: failed to load aircraft database: {e}")

    def process(self, aircraft: list[Aircraft]) -> list[Aircraft]:
        if not self._target_registrations:
            return []

        filtered = []
        for a in aircraft:
            hex_code = (a.meta.icao_hex or "").strip().upper()
            reg = (a.airframe.registration or "").strip().upper()

            match_hex = hex_code in self._target_hexes
            match_reg = reg in self._target_registrations

            if match_hex or match_reg:
                if not a.airframe.registration:
                    a.airframe.registration = self._hex_to_reg.get(hex_code) or (reg if reg in self._target_registrations else list(self._target_registrations)[0])
                if not a.airframe.aircraft_type and hex_code in self._hex_to_type:
                    a.airframe.aircraft_type = self._hex_to_type[hex_code]
                filtered.append(a)

        return filtered


def get(cfg: dict) -> RegistrationFilter:
    from config import config as squawk_config
    target_registrations = cfg.get("registrations", [])
    data_dir = Path(squawk_config.squawk.data_dir)
    return RegistrationFilter(target_registrations=target_registrations, data_dir=data_dir)
