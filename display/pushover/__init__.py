"""
display/pushover/__init__.py

Display module that sends a Pushover notification for the closest aircraft
with its registration, type, origin, and destination.
Restricted to send at most once every 15 minutes via a disk-based timestamp file.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
import requests

from modules import BaseModule
from schemas.aircraft import Aircraft


class PushoverDisplay(BaseModule):

    def __init__(self, cfg: dict) -> None:
        from config import config as squawk_config
        self._token = cfg.get("token")
        self._user = cfg.get("user")
        self._data_dir = Path(cfg.get("data_dir", squawk_config.squawk.data_dir))
        self._last_sent_path = self._data_dir / "display" / "pushover" / "last_notification.txt"
        self._last_json_path = self._data_dir / "display" / "pushover" / "last_notification.json"

    def process(self, aircraft: list[Aircraft]) -> list[Aircraft]:
        if not aircraft:
            return aircraft

        # If credentials are not set or are placeholders, skip sending
        if not self._token or not self._user or self._token == "xxxxxxxxxxxxxxxxxxxxxxxxxx" or self._user == "xxxxxxxxxxxxxxxxxxxxxxxxxx":
            print("Pushover display credentials not configured. Skipping notification.")
            return aircraft

        a = aircraft[0]
        origin = a.route.origin_iata
        dest = a.route.destination_iata

        # Suppress notifications if origin or destination airport is missing
        if not origin or not dest:
            return aircraft

        airline = (a.route.airline_name or a.airframe.operator or "").strip()
        reg = a.airframe.registration or a.route.callsign or a.meta.icao_hex or "???"
        typ = a.airframe.aircraft_type or "???"

        origin_str = f"{origin} ({a.route.origin_country})" if a.route.origin_country else origin
        dest_str = f"{dest} ({a.route.destination_country})" if a.route.destination_country else dest

        if airline:
            message = f"{airline} {reg} {typ} {origin_str} -> {dest_str}"
        else:
            message = f"{reg} {typ} {origin_str} -> {dest_str}"

        # Rate limiting: 15 minutes (900 seconds) cooldown between notifications
        now = time.time()
        hex_code = (a.meta.icao_hex or "").strip().upper()
        if not self._can_send(now, hex_code):
            # print("Pushover notification rate limit active (15m). Skipping.")
            return aircraft

        # Attempt to send notification
        try:
            resp = requests.post(
                "https://api.pushover.net/1/messages.json",
                data={
                    "token": self._token,
                    "user": self._user,
                    "message": message,
                },
                timeout=5
            )
            resp.raise_for_status()
            self._write_last_sent(now, hex_code, message)
            print(f"Pushover notification sent: {message}")
        except Exception as e:
            print(f"Failed to send Pushover notification: {e}")

        return aircraft

    def _can_send(self, now: float, hex_code: str = "") -> bool:
        if not self._last_sent_path.exists() and not self._last_json_path.exists():
            return True
        try:
            last_sent_time = 0.0

            if self._last_json_path.exists():
                data = json.loads(self._last_json_path.read_text(encoding="utf-8"))
                last_sent_time = float(data.get("timestamp", 0))
            elif self._last_sent_path.exists():
                content = self._last_sent_path.read_text(encoding="utf-8").strip()
                if content:
                    last_sent_time = float(content)

            return (now - last_sent_time) >= 900.0
        except Exception as e:
            print(f"Error reading last notification time: {e}")
            return True

    def _write_last_sent(self, now: float, hex_code: str = "", message: str = "") -> None:
        try:
            self._last_sent_path.parent.mkdir(parents=True, exist_ok=True)
            self._last_sent_path.write_text(str(now), encoding="utf-8")
            data = {
                "timestamp": now,
                "hex": hex_code,
                "message": message,
            }
            tmp = self._last_json_path.with_name(self._last_json_path.name + ".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(self._last_json_path)
        except Exception as e:
            print(f"Error writing last notification state: {e}")


def get(cfg: dict) -> PushoverDisplay:
    return PushoverDisplay(cfg)
