"""
display/pushover/__init__.py

Display module that sends a Pushover notification for the closest aircraft
with its registration, type, origin, and destination.
Restricted to send at most once every 15 minutes via a disk-based timestamp file.
"""

from __future__ import annotations

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

    def process(self, aircraft: list[Aircraft]) -> list[Aircraft]:
        if not aircraft:
            return aircraft

        # If credentials are not set or are placeholders, skip sending
        if not self._token or not self._user or self._token == "xxxxxxxxxxxxxxxxxxxxxxxxxx" or self._user == "xxxxxxxxxxxxxxxxxxxxxxxxxx":
            print("Pushover display credentials not configured. Skipping notification.")
            return aircraft

        # Format message with registration, type, from, and to details
        a = aircraft[0]
        reg = a.airframe.registration or a.route.callsign or a.meta.icao_hex or "???"
        typ = a.airframe.aircraft_type or "???"
        origin = a.route.origin_iata or "???"
        dest = a.route.destination_iata or "???"

        message = f"{reg} {typ} {origin} {dest}"

        # Rate limiting: 15 minutes (900 seconds)
        now = time.time()
        if not self._can_send(now):
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
            self._write_last_sent(now)
            print(f"Pushover notification sent: {message}")
        except Exception as e:
            print(f"Failed to send Pushover notification: {e}")

        return aircraft

    def _can_send(self, now: float) -> bool:
        if not self._last_sent_path.exists():
            return True
        try:
            content = self._last_sent_path.read_text(encoding="utf-8").strip()
            if not content:
                return True
            last_sent_time = float(content)
            return (now - last_sent_time) >= 900.0
        except Exception as e:
            print(f"Error reading last notification time from {self._last_sent_path}: {e}")
            return True

    def _write_last_sent(self, now: float) -> None:
        try:
            self._last_sent_path.parent.mkdir(parents=True, exist_ok=True)
            self._last_sent_path.write_text(str(now), encoding="utf-8")
        except Exception as e:
            print(f"Error writing last notification time to {self._last_sent_path}: {e}")


def get(cfg: dict) -> PushoverDisplay:
    return PushoverDisplay(cfg)
