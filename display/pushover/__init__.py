"""
display/pushover/__init__.py

Display module that sends a Pushover notification for the closest aircraft
with its registration, type, origin, and destination.
Restricted to send at most once per flight identifier (hex + callsign) within
a configurable cooldown window (default 2 hours) via a disk-based JSON state file.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
import requests

from modules import BaseModule
from schemas.aircraft import Aircraft


def _format_country(c: str | None) -> str | None:
    if not c:
        return None
    s = c.strip()
    if s.lower() in ("united kingdom", "uk"):
        return "UK"
    return s


class PushoverDisplay(BaseModule):

    def __init__(self, cfg: dict) -> None:
        from config import config as squawk_config
        self._token = cfg.get("token")
        self._user = cfg.get("user")
        self._cooldown_seconds = float(cfg.get("cooldown_seconds", 7200))
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

        airline = (a.route.airline_name or a.airframe.operator or "").strip()
        reg = (a.airframe.registration or "").strip()
        callsign = (a.route.callsign or "").strip().upper()
        typ = (a.airframe.aircraft_type or "").strip()
        origin = (a.route.origin_iata or "").strip()
        dest = (a.route.destination_iata or "").strip()

        # Pushover to ignore anything without all 5 facts
        # (Airline, Registration, Callsign, Aircraft type, Route origin/destination)
        if not (airline and reg and callsign and typ and origin and dest):
            return aircraft

        origin_c = _format_country(a.route.origin_country)
        dest_c = _format_country(a.route.destination_country)

        origin_str = f"{origin} ({origin_c})" if origin_c else origin
        dest_str = f"{dest} ({dest_c})" if dest_c else dest

        # Format: Airline Registration [callsign] Aircraft  :  Route
        message = f"{airline} {reg} [{callsign}] {typ}  :  {origin_str} -> {dest_str}"

        # Rate limiting by hex + callsign flight identifier
        now = time.time()
        hex_code = (a.meta.icao_hex or "").strip().upper()
        if not self._can_send(now, hex_code, callsign):
            # print("Pushover notification rate limit active. Skipping.")
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
            self._write_last_sent(now, hex_code, callsign, message)
            print(f"Pushover notification sent: {message}")
        except Exception as e:
            print(f"Failed to send Pushover notification: {e}")

        return aircraft

    def _get_key(self, hex_code: str, callsign: str) -> str:
        h = (hex_code or "").strip().upper()
        c = (callsign or "").strip().upper()
        return f"{h}_{c}"

    def _can_send(self, now: float, hex_code: str = "", callsign: str = "") -> bool:
        if not self._last_sent_path.exists() and not self._last_json_path.exists():
            return True
        try:
            key = self._get_key(hex_code, callsign)
            empty_key = self._get_key(hex_code, "")
            last_sent_time = 0.0

            if self._last_json_path.exists():
                data = json.loads(self._last_json_path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and "entries" in data and isinstance(data["entries"], dict):
                    entries = data["entries"]
                    if key in entries and isinstance(entries[key], dict):
                        last_sent_time = max(last_sent_time, float(entries[key].get("timestamp", 0)))
                    # Suppress duplicate if hex was notified with empty callsign < 15 min ago
                    if callsign and empty_key in entries and isinstance(entries[empty_key], dict):
                        empty_ts = float(entries[empty_key].get("timestamp", 0))
                        if (now - empty_ts) < 900.0:
                            last_sent_time = max(last_sent_time, empty_ts)
                elif isinstance(data, dict) and "timestamp" in data:
                    legacy_hex = (data.get("hex") or "").strip().upper()
                    if not legacy_hex or legacy_hex == (hex_code or "").strip().upper():
                        last_sent_time = float(data.get("timestamp", 0))
            elif self._last_sent_path.exists():
                content = self._last_sent_path.read_text(encoding="utf-8").strip()
                if content:
                    last_sent_time = float(content)

            return (now - last_sent_time) >= self._cooldown_seconds
        except Exception as e:
            print(f"Error reading last notification time: {e}")
            return True

    def _write_last_sent(self, now: float, hex_code: str = "", callsign: str = "", message: str = "") -> None:
        try:
            self._last_sent_path.parent.mkdir(parents=True, exist_ok=True)
            self._last_sent_path.write_text(str(now), encoding="utf-8")

            key = self._get_key(hex_code, callsign)
            entries = {}

            if self._last_json_path.exists():
                try:
                    data = json.loads(self._last_json_path.read_text(encoding="utf-8"))
                    if isinstance(data, dict) and "entries" in data and isinstance(data["entries"], dict):
                        entries = data["entries"]
                except Exception:
                    entries = {}

            # Prune entries older than cooldown_seconds
            cutoff = now - self._cooldown_seconds
            entries = {
                k: v for k, v in entries.items()
                if isinstance(v, dict) and float(v.get("timestamp", 0)) >= cutoff
            }

            entries[key] = {
                "timestamp": now,
                "hex": hex_code,
                "callsign": callsign,
                "message": message,
            }

            out_data = {
                "timestamp": now,
                "hex": hex_code,
                "callsign": callsign,
                "message": message,
                "entries": entries,
            }

            tmp = self._last_json_path.with_name(self._last_json_path.name + ".tmp")
            tmp.write_text(json.dumps(out_data, indent=2), encoding="utf-8")
            tmp.replace(self._last_json_path)
        except Exception as e:
            print(f"Error writing last notification state: {e}")


def get(cfg: dict) -> PushoverDisplay:
    return PushoverDisplay(cfg)

