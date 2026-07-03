"""
modules/registration_filter.py

Processor module that fetches a target registration from a URL every 15 minutes,
resolves it to a hex code via the tar1090 database, and filters aircraft.
"""

from __future__ import annotations

import csv
import time
from pathlib import Path
import requests

from modules import BaseModule
from schemas.aircraft import Aircraft


class RegistrationFilter(BaseModule):

    def __init__(self, url: str, data_dir: Path) -> None:
        self._url = url
        self._data_dir = data_dir
        self._reg_file = data_dir / "modules" / "registration_filter" / "registration.txt"
        self._last_check_file = data_dir / "modules" / "registration_filter" / "last_check.txt"
        self._csv_path = data_dir / "modules" / "tar1090_db" / "aircraft.csv"
        self._reg_to_hex: dict[str, str] = {}
        self._last_fetched_reg: str | None = None

        # Resolve tar1090_db download/existence
        if not self._csv_path.exists():
            from modules.tar1090_db import _download
            try:
                _download(self._csv_path)
            except Exception as e:
                print(f"registration_filter: failed to download tar1090_db database: {e}")

        self._load_reg_to_hex()
        self._load_cached_reg()

    def _load_reg_to_hex(self) -> None:
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
                    if reg and hex_code:
                        self._reg_to_hex[reg] = hex_code
        except Exception as e:
            print(f"registration_filter: failed to load aircraft database: {e}")

    def _load_cached_reg(self) -> None:
        if not self._reg_file.exists():
            return
        try:
            content = self._reg_file.read_text(encoding="utf-8").strip()
            if content:
                self._last_fetched_reg = content.upper()
        except Exception as e:
            print(f"registration_filter: failed to read cached registration: {e}")

    def process(self, aircraft: list[Aircraft]) -> list[Aircraft]:
        now = time.time()
        if self._should_fetch(now):
            self._fetch_registration(now)

        if not self._last_fetched_reg:
            return []

        target_reg = self._last_fetched_reg
        target_hex = self._reg_to_hex.get(target_reg)

        filtered = []
        for a in aircraft:
            hex_code = (a.meta.icao_hex or "").strip().upper()
            reg = (a.airframe.registration or "").strip().upper()

            match_hex = (target_hex is not None and hex_code == target_hex)
            match_reg = (reg == target_reg)

            if match_hex or match_reg:
                filtered.append(a)

        return filtered

    def _should_fetch(self, now: float) -> bool:
        if not self._last_check_file.exists():
            return True
        try:
            content = self._last_check_file.read_text(encoding="utf-8").strip()
            if not content:
                return True
            last_check = float(content)
            return (now - last_check) >= 900.0
        except Exception:
            return True

    def _fetch_registration(self, now: float) -> None:
        try:
            resp = requests.get(self._url, timeout=10)
            resp.raise_for_status()
            reg = resp.text.strip().upper()
            if reg:
                self._last_fetched_reg = reg
                self._write_cached_reg(reg, now)
                print(f"registration_filter: updated target registration to {reg}")
        except Exception as e:
            print(f"registration_filter: failed to fetch registration from URL: {e}")

    def _write_cached_reg(self, reg: str, now: float) -> None:
        try:
            self._reg_file.parent.mkdir(parents=True, exist_ok=True)
            self._reg_file.write_text(reg, encoding="utf-8")
            self._last_check_file.write_text(str(now), encoding="utf-8")
        except Exception as e:
            print(f"registration_filter: failed to save registration cache: {e}")


def get(cfg: dict) -> RegistrationFilter:
    from config import config as squawk_config
    url = cfg.get("url", "https://www.wapentake.uk/aircraft.php")
    data_dir = Path(squawk_config.squawk.data_dir)
    return RegistrationFilter(url=url, data_dir=data_dir)
