"""plugins/adsbdb.py

Enriches Aircraft records from adsbdb.com.

Data sources (please honour these credits):

  Aircraft data    — Planebase             https://planebase.biz/
  Aircraft photos  — airport-data.com      https://airport-data.com/
  Flight route data — David Taylor (Edinburgh) and Jim Mason (Glasgow).
                      May not be copied, published, or incorporated into
                      other databases without the explicit permission of
                      David J Taylor, Edinburgh.

  API hosting      — adsbdb.com            https://www.adsbdb.com/

This plugin treats the local cache as an ephemeral working copy, not a
republished database. It respects adsbdb's published rate limits in
code. If you fork Squawk for anything beyond personal hobby use,
contact the upstream maintainers before scaling traffic or persisting
their data.

Run AFTER filters that narrow the aircraft list (e.g. closest_filter).
Running before filters means every aircraft in range triggers a
cache/API lookup every cycle, which both wastes calls and risks
exhausting the adsbdb rate budget.

Endpoint:
  GET https://api.adsbdb.com/v0/aircraft/<HEX>?callsign=<CALLSIGN>

  Returns BOTH aircraft and flightroute blocks in one call. Aircraft data
  for an airframe arrives bundled with each route lookup, so no separate
  long-term aircraft cache is needed.

Skip when:
  route.callsign is UNKNOWN — without a callsign we cannot do a route
  lookup. Airframe data (reg, type) is still populated by tar1090_db for
  these aircraft; we only lose the registered_owner field, which is
  usually a private individual for callsign-less aircraft.

Cache (cache-first, then API):
  data/plugins/adsbdb/<CALLSIGN>.json    TTL 1 hour

  Stores the full adsbdb response. A cache hit means zero API calls.
  Stale files trigger a re-fetch; on fetch failure, the stale file is
  used. 404 responses are cached as not-found markers so we don't retry.

Rate limits (rolling windows, enforced in-memory):
   512 calls / 60 seconds
  1024 calls / 300 seconds

  When near either limit the call is skipped this cycle, leaving fields
  as UNKNOWN. Next cycle tries again.
"""

from __future__ import annotations

import json
import os
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

from plugins import BasePlugin
from schemas.aircraft import Aircraft


_API_BASE          = "https://api.adsbdb.com/v0/aircraft"
_CACHE_TTL_SECONDS = 3600
_RATE_60S          = 512
_RATE_300S         = 1024
_TIMEOUT_SECONDS   = 5
_HEADERS           = {"User-Agent": "Squawk/1.1 (+https://github.com/IoanaLogafatu/squawk)"}


class AdsbdbEnricher(BasePlugin):

    def __init__(self, cache_dir: Path) -> None:
        self._cache_dir = cache_dir
        self._call_times: deque[float] = deque()

    def process(self, aircraft: list[Aircraft]) -> list[Aircraft]:
        for a in aircraft:
            callsign = (a.route.callsign or "").strip().upper()
            if not callsign:
                continue
            data = self._get(a.meta.icao_hex.upper(), callsign)
            if not data or data.get("not_found"):
                continue
            self._apply(a, data)
        return aircraft

    def _get(self, hex_id: str, callsign: str) -> Optional[dict]:
        cache_path = self._cache_dir / f"{callsign}.json"
        cached_data: Optional[dict] = None
        cache_fresh = False

        if cache_path.exists():
            try:
                cached_data = json.loads(cache_path.read_text(encoding="utf-8"))
                age = time.time() - cache_path.stat().st_mtime
                cache_fresh = age <= _CACHE_TTL_SECONDS
            except Exception:
                pass

        if cache_fresh and cached_data is not None:
            # print(f"  adsbdb: cache hit for {callsign}")
            return cached_data

        if not self._under_rate_limit():
            print(f"  adsbdb: rate limit reached — skipping {callsign}")
            return None

        fetched = self._fetch(hex_id, callsign)
        if fetched is not None:
            tmp = cache_path.with_name(cache_path.name + ".tmp")
            tmp.write_text(json.dumps(fetched, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, cache_path)
            return fetched

        if cached_data is not None:
            print(f"  adsbdb: fetch failed — using stale cache for {callsign}")
        return cached_data

    def _fetch(self, hex_id: str, callsign: str) -> Optional[dict]:
        url = f"{_API_BASE}/{hex_id}?callsign={callsign}"
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT_SECONDS)
        except Exception as exc:
            self._record_call()
            print(f"  adsbdb: error fetching {callsign}: {exc}")
            return None

        self._record_call()

        if resp.status_code == 200:
            try:
                data = resp.json()
            except Exception as exc:
                print(f"  adsbdb: malformed JSON for {callsign}: {exc}")
                return None
            print(f"  adsbdb: 200 for {callsign}")
            return data.get("response", data)

        if resp.status_code == 404:
            print(f"  adsbdb: 404 for {callsign}")
            return {
                "not_found": True,
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }

        print(f"  adsbdb: unexpected status {resp.status_code} for {callsign}")
        return None

    def _under_rate_limit(self) -> bool:
        now = time.monotonic()
        while self._call_times and now - self._call_times[0] > 300:
            self._call_times.popleft()
        if len(self._call_times) >= _RATE_300S:
            return False
        count_60s = sum(1 for t in self._call_times if now - t <= 60)
        if count_60s >= _RATE_60S:
            return False
        return True

    def _record_call(self) -> None:
        self._call_times.append(time.monotonic())

    def _apply(self, aircraft: Aircraft, data: dict) -> None:
        aircraft.raw.adsbdb = data

        ac = data.get("aircraft") or {}
        if aircraft.airframe.manufacturer is None and ac.get("manufacturer"):
            aircraft.airframe.manufacturer = ac["manufacturer"]
        if aircraft.airframe.registration is None and ac.get("registration"):
            aircraft.airframe.registration = ac["registration"]
        if aircraft.airframe.aircraft_type is None and ac.get("type"):
            aircraft.airframe.aircraft_type = ac["type"]
        if aircraft.airframe.operator is None and ac.get("registered_owner"):
            aircraft.airframe.operator = ac["registered_owner"]

        fr = data.get("flightroute") or {}
        airline = fr.get("airline") or {}
        origin  = fr.get("origin") or {}
        dest    = fr.get("destination") or {}

        if aircraft.route.airline_name is None and airline.get("name"):
            aircraft.route.airline_name = airline["name"]
        if aircraft.route.airline_country is None and airline.get("country"):
            aircraft.route.airline_country = airline["country"]
        if aircraft.route.origin_iata is None and origin.get("iata_code"):
            aircraft.route.origin_iata = origin["iata_code"]
        if aircraft.route.origin_name is None and origin.get("name"):
            aircraft.route.origin_name = origin["name"]
        if aircraft.route.origin_country is None and origin.get("country_name"):
            aircraft.route.origin_country = origin["country_name"]
        if aircraft.route.destination_iata is None and dest.get("iata_code"):
            aircraft.route.destination_iata = dest["iata_code"]
        if aircraft.route.destination_name is None and dest.get("name"):
            aircraft.route.destination_name = dest["name"]
        if aircraft.route.destination_country is None and dest.get("country_name"):
            aircraft.route.destination_country = dest["country_name"]


def get(cfg: dict) -> AdsbdbEnricher:
    from config import config as squawk_config
    data_dir  = Path(squawk_config.squawk.data_dir)
    cache_dir = data_dir / "plugins" / "adsbdb"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return AdsbdbEnricher(cache_dir=cache_dir)
