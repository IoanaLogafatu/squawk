"""modules/adsbdb.py

Enriches Aircraft records from adsbdb.com.

Data sources (please honour these credits):

  Aircraft data    — Planebase             https://planebase.biz/
  Aircraft photos  — airport-data.com      https://airport-data.com/
  Flight route data — David Taylor (Edinburgh) and Jim Mason (Glasgow).
                      May not be copied, published, or incorporated into
                      other databases without the explicit permission of
                      David J Taylor, Edinburgh.

  API hosting      — adsbdb.com            https://www.adsbdb.com/

This module treats the local cache as an ephemeral working copy, not a
republished database. It respects adsbdb's published rate limits in
code. If you fork Squawk for anything beyond personal hobby use,
contact the upstream maintainers before scaling traffic or persisting
their data.

Run AFTER filters that narrow the aircraft list (e.g. closest_filter).
Running before filters means every aircraft in range triggers a
cache/API lookup every cycle, which both wastes calls and risks
exhausting the adsbdb rate budget.

Endpoints — two, looked up independently:

  GET https://api.adsbdb.com/v0/aircraft/<HEX>       → airframe
  GET https://api.adsbdb.com/v0/callsign/<CALLSIGN>  → today's route

  These are separate lookups because they fail separately. An airframe's
  registration, type and manufacturer are immutable for the life of the
  aircraft; a callsign's route is a property of today's flight. Asking
  for both in one call meant a hex the database did not recognise took
  the route down with it — and a community airframe database is most
  likely to be missing exactly the newest registrations. Whichever half
  answers is applied.

  The aircraft endpoint signals an unknown hex as the *string*
  "unknown aircraft" under `response` rather than a 404, so a string
  response is treated as a definitive miss.

Skip when:
  meta.icao_hex is UNKNOWN — there is nothing to look up.
  meta.icao_hex starts with '~' — readsb's marker for a non-ICAO
  address (TIS-B relay, anonymised target). No airframe exists behind
  one, so both lookups are skipped entirely.
  airframe.db_flags has PIA set — a Privacy ICAO Address is temporary
  and identifies no airframe. Both lookups skipped, as for '~'.
  airframe.db_flags has LADD set — the FAA's Limiting Aircraft Data
  Displayed programme. The ROUTE lookup only is skipped, and logged as
  'suppressed'; the airframe lookup still runs, because LADD suppresses
  flight data rather than the airframe record. Asking a route API about
  an aircraft whose operator has formally requested suppression is
  wrong on its own terms, independent of what it costs.
  Military and interesting flags suppress nothing — they are
  descriptive, and a military airframe is often in adsbdb.
  The route lookup is additionally skipped when route.callsign is
  UNKNOWN, which is the common case for aircraft that have not yet
  transmitted identity.

Cache (cache-first, then API), under the module directory:
  aircraft/<HEX>.json       TTL 7 days   — airframe data does not change
  route/<CALLSIGN>.json     TTL 1 hour   — a route is per-flight

  A cache hit means zero API calls. Stale files trigger a re-fetch; on
  fetch failure the stale file is used. Definitive misses (404, or an
  "unknown" string response) are cached as not-found markers so they are
  not retried every cycle. A timeout or a 500 is never recorded as a
  miss — those are transient and must stay retryable.

  In-memory memo (60 seconds), in front of the disk cache, keyed
  separately per space as ("aircraft", hex) / ("route", callsign). When
  several chains process the same aircraft in the same cycle, one
  performs each lookup and the rest reuse its result. Failed and
  rate-limited lookups are memoised for the same window so they are not
  retried by every chain.

Rate limits (rolling windows, enforced in-memory):
   512 calls / 60 seconds
  1024 calls / 300 seconds

  When near either limit the call is skipped this cycle, leaving fields
  as UNKNOWN. Next cycle tries again.

Config keys:
  log_unresolved (bool, default false) — append one JSON line to
  route/unresolved.jsonl for each aircraft whose route adsbdb could not
  resolve. Records what *adsbdb* could not resolve, not what the
  pipeline as a whole failed to; a later fallback enricher may fill some
  of these in.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

from modules import BaseModule, ModuleContext
from schemas.aircraft import Aircraft


_API_AIRCRAFT = "https://api.adsbdb.com/v0/aircraft"
_API_CALLSIGN = "https://api.adsbdb.com/v0/callsign"

_AIRCRAFT_TTL_SECONDS = 604800   # 7 days — airframe data does not change
_ROUTE_TTL_SECONDS    = 3600     # 1 hour — a route is a property of today's flight

_RATE_60S        = 512
_RATE_300S       = 1024
_TIMEOUT_SECONDS = 5
_HEADERS         = {"User-Agent": "Squawk/1.1 (+https://github.com/IoanaLogafatu/squawk)"}

_MEMO_TTL_SECONDS = 60

_AIRCRAFT = "aircraft"
_ROUTE    = "route"

# tar1090 dbFlags bits this module acts on. Military (1) and interesting (2)
# are descriptive rather than privacy flags and suppress nothing.
_FLAG_PIA  = 4   # Privacy ICAO Address — temporary, identifies nothing
_FLAG_LADD = 8   # FAA Limiting Aircraft Data Displayed — route withheld by request

_TTLS = {
    _AIRCRAFT: _AIRCRAFT_TTL_SECONDS,
    _ROUTE:    _ROUTE_TTL_SECONDS,
}

_UNRESOLVED_LOG = "unresolved.jsonl"


def _not_found_marker() -> dict:
    return {
        "not_found":  True,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


class AdsbdbEnricher(BaseModule):

    def __init__(self, cache_dir: Path, log_unresolved: bool = False) -> None:
        self._cache_dir = cache_dir
        self._log_unresolved = bool(log_unresolved)

        self._call_times: deque[float] = deque()
        self._rate_lock = threading.Lock()

        self._memo: dict[tuple[str, str], tuple[float, dict | None]] = {}
        self._key_locks: dict[tuple[str, str], threading.Lock] = {}
        self._memo_lock = threading.Lock()

        # (hex, callsign) already written to the unresolved log this process.
        self._unresolved_seen: set[tuple[str, str | None]] = set()
        self._unresolved_lock = threading.Lock()

    def process(self, aircraft: list[Aircraft]) -> list[Aircraft]:
        self._sweep()
        for a in aircraft:
            hex_id = (a.meta.icao_hex or "").strip().upper()
            if not hex_id:
                continue

            # readsb prefixes an address with '~' when it is not a real ICAO
            # 24-bit address — TIS-B relays, anonymised targets. There is no
            # airframe behind one and never will be, so a lookup is a
            # guaranteed miss whose 404 would cache a not-found marker keyed on
            # something that is not an aircraft identifier.
            #
            # Deliberately not logged as unresolved: the log records what adsbdb
            # could not resolve, and a non-ICAO address was never a candidate.
            # Logging it would record a decision Squawk made rather than a gap
            # in adsbdb's data. These aircraft still flow through the pipeline
            # and still display — they simply carry no enrichment.
            if hex_id.startswith("~"):
                continue

            # db_flags is None when nothing has told us — a receiver without
            # --db-file, or an aircraft absent from the CSV. Absence of
            # information is not information, so None suppresses nothing.
            flags = a.airframe.db_flags
            pia   = flags is not None and flags & _FLAG_PIA
            ladd  = flags is not None and flags & _FLAG_LADD

            # PIA: a Privacy ICAO Address is temporary and identifies no
            # airframe, so neither lookup can succeed. Treated exactly like a
            # '~' address, log line included: it was never a candidate.
            if pia:
                continue

            callsign = (a.route.callsign or "").strip().upper() or None

            # Two independent lookups. Either may miss without affecting the
            # other — that separation is the whole point of this module.
            merged: dict = {}

            airframe = self._get(_AIRCRAFT, hex_id)
            if isinstance(airframe, dict) and not airframe.get("not_found"):
                merged.update(airframe)

            # LADD suppresses the *route* only, and the asymmetry is the point.
            # The FAA programme withholds flight data; the airframe record is
            # not suppressed and is often present. Skipping both lookups would
            # throw away registration, type and operator that adsbdb would give
            # for free — and this module has already seen the reverse case,
            # where a 404 on the airframe still yielded a full route. The two
            # fail independently and must be skipped independently.
            route: Optional[dict] = None
            if not ladd and callsign:
                route = self._get(_ROUTE, callsign)
                if isinstance(route, dict) and not route.get("not_found"):
                    merged.update(route)

            if merged:
                self._apply(a, merged)

            if self._log_unresolved and "flightroute" not in merged:
                self._record_unresolved(a, hex_id, callsign, route, suppressed=bool(ladd))

        return aircraft

    # -----------------------------------------------------------------
    # Lookup — memo in front of disk cache in front of API
    # -----------------------------------------------------------------

    def _get(self, kind: str, key: str) -> Optional[dict]:
        memo_key = (kind, key)
        now = time.monotonic()

        with self._memo_lock:
            entry = self._memo.get(memo_key)
            if entry is not None and now - entry[0] <= _MEMO_TTL_SECONDS:
                return entry[1]
            key_lock = self._key_locks.setdefault(memo_key, threading.Lock())

        with key_lock:
            # Re-check: another thread may have filled the memo while we waited
            # on the lock, which is the whole point of this method.
            with self._memo_lock:
                entry = self._memo.get(memo_key)
                if entry is not None and time.monotonic() - entry[0] <= _MEMO_TTL_SECONDS:
                    return entry[1]

            result = self._get_uncached(kind, key)

            with self._memo_lock:
                self._memo[memo_key] = (time.monotonic(), result)
            return result

    def _sweep(self) -> None:
        now = time.monotonic()
        with self._memo_lock:
            dead = [k for k, (t, _) in self._memo.items()
                    if now - t > _MEMO_TTL_SECONDS]
            for k in dead:
                del self._memo[k]
                self._key_locks.pop(k, None)

    def _get_uncached(self, kind: str, key: str) -> Optional[dict]:
        cache_path = self._cache_dir / kind / f"{key}.json"
        ttl = _TTLS[kind]

        cached_data: Optional[dict] = None
        cache_fresh = False

        if cache_path.exists():
            try:
                cached_data = json.loads(cache_path.read_text(encoding="utf-8"))
                age = time.time() - cache_path.stat().st_mtime
                cache_fresh = age <= ttl
            except Exception:
                pass

        if cache_fresh and isinstance(cached_data, dict):
            return cached_data

        fetched = (self._fetch_aircraft(key) if kind == _AIRCRAFT
                   else self._fetch_route(key))
        if isinstance(fetched, dict):
            self._save_cache(kind, key, fetched)
            return fetched

        if isinstance(cached_data, dict):
            print(f"  adsbdb: fetch failed — using stale {kind} cache for {key}")
            return cached_data

        return None

    def _save_cache(self, kind: str, key: str, data: dict) -> None:
        directory = self._cache_dir / kind
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{key}.json"
        tmp = path.with_name(f"{path.name}.{threading.get_ident()}_{time.time_ns()}.tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            if tmp.exists():
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass

    # -----------------------------------------------------------------
    # Fetch — one method per endpoint
    # -----------------------------------------------------------------

    def _fetch_aircraft(self, hex_id: str) -> Optional[dict]:
        """Airframe for one hex. Returns {"aircraft": {...}}, a not-found
        marker, or None when the call failed in a way worth retrying."""
        status, res = self._request(f"{_API_AIRCRAFT}/{hex_id}", hex_id)
        if status == "not_found":
            return _not_found_marker()
        if status != "ok":
            return None

        # The unknown-hex case arrives as a 200 whose response is the string
        # "unknown aircraft", not a 404. Definitive either way.
        if isinstance(res, str):
            print(f"  adsbdb: aircraft {hex_id} unknown ({res})")
            return _not_found_marker()
        if isinstance(res, dict) and "aircraft" in res:
            print(f"  adsbdb: 200 aircraft for {hex_id}")
            return {"aircraft": res["aircraft"]}

        print(f"  adsbdb: 200 aircraft for {hex_id} but unexpected shape: {res}")
        return None

    def _fetch_route(self, callsign: str) -> Optional[dict]:
        """Route for one callsign. Returns {"flightroute": {...}}, a not-found
        marker, or None when the call failed in a way worth retrying."""
        status, res = self._request(f"{_API_CALLSIGN}/{callsign}", callsign)
        if status == "not_found":
            return _not_found_marker()
        if status != "ok":
            return None

        if isinstance(res, str):
            print(f"  adsbdb: callsign {callsign} unknown ({res})")
            return _not_found_marker()
        if isinstance(res, dict) and "flightroute" in res:
            print(f"  adsbdb: 200 route for {callsign}")
            return {"flightroute": res["flightroute"]}

        print(f"  adsbdb: 200 route for {callsign} but unexpected shape: {res}")
        return None

    def _request(self, url: str, lbl: str) -> tuple[str, object]:
        """One HTTP call, reduced to a three-way outcome.

        ("ok", response)  — 200, response envelope unwrapped
        ("not_found", None) — 404; the only cacheable failure
        ("fail", None)      — rate limited, transport error, bad JSON, or any
                              other status. Transient: never cached as a miss.
        """
        if not self._try_acquire():
            print(f"  adsbdb: rate limit reached — skipping {lbl}")
            return ("fail", None)

        try:
            resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT_SECONDS)
        except Exception as exc:
            print(f"  adsbdb: error fetching {lbl}: {exc}")
            return ("fail", None)

        # Identity check, not truthiness: requests.Response.__bool__ is `self.ok`,
        # so any 4xx/5xx is falsy. Testing `not resp` here would swallow the 404
        # before the status check below and never cache a definitive miss.
        if resp is None or not hasattr(resp, "status_code"):
            return ("fail", None)

        if resp.status_code == 200:
            try:
                data = resp.json()
            except Exception as exc:
                print(f"  adsbdb: malformed JSON for {lbl}: {exc}")
                return ("fail", None)
            res = data.get("response", data) if isinstance(data, dict) else data
            return ("ok", res)

        if resp.status_code == 404:
            print(f"  adsbdb: 404 for {lbl}")
            return ("not_found", None)

        print(f"  adsbdb: unexpected status {resp.status_code} for {lbl}")
        return ("fail", None)

    def _try_acquire(self) -> bool:
        """Reserve one API call slot. Returns False if either rate limit is reached."""
        with self._rate_lock:
            now = time.monotonic()
            while self._call_times and now - self._call_times[0] > 300:
                self._call_times.popleft()
            if len(self._call_times) >= _RATE_300S:
                return False
            if sum(1 for t in self._call_times if now - t <= 60) >= _RATE_60S:
                return False
            self._call_times.append(now)
            return True

    # -----------------------------------------------------------------
    # Unresolved-route diagnostic log
    # -----------------------------------------------------------------

    def _record_unresolved(self, aircraft: Aircraft, hex_id: str,
                           callsign: str | None, route: Optional[dict],
                           suppressed: bool = False) -> None:
        """Append one line per (hex, callsign) whose route adsbdb could not give us.

        The reasons have different fixes, so they are distinguished:
        no_callsign      — nothing to look up
        unknown_callsign — adsbdb answered, definitively, that it has no route
        fetch_failed     — timeout, rate limit or non-404 error; transient
        suppressed       — LADD: a route that plausibly exists, deliberately
                           withheld. Worth counting, because it is a real gap in
                           what the wall can show. PIA and '~' addresses get no
                           line at all — they were never candidates for anything.
        """
        # Checked here, at the point of writing, not inside the memo-guarded
        # lookup — so it holds for the life of the process rather than for one
        # memo window. The two lifetimes are deliberately different: the
        # on-disk not-found marker expires so the route is retried (a flight
        # absent from adsbdb today may be there tomorrow), while the *logging*
        # happens once per run. Collapsing them is the obvious wrong fix.
        seen_key = (hex_id, callsign)
        with self._unresolved_lock:
            if seen_key in self._unresolved_seen:
                return
            self._unresolved_seen.add(seen_key)

        # 'suppressed' wins over 'no_callsign': we would not have asked either
        # way, and which flag stopped us is the more useful fact.
        if suppressed:
            reason = "suppressed"
        elif callsign is None:
            reason = "no_callsign"
        elif isinstance(route, dict) and route.get("not_found"):
            reason = "unknown_callsign"
        else:
            reason = "fetch_failed"

        line = json.dumps({
            "at":           datetime.now(timezone.utc).isoformat(),
            "hex":          hex_id,
            "callsign":     callsign,
            "registration": aircraft.airframe.registration,
            "reason":       reason,
        }, ensure_ascii=False)

        directory = self._cache_dir / _ROUTE
        try:
            directory.mkdir(parents=True, exist_ok=True)
            with open(directory / _UNRESOLVED_LOG, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as exc:
            print(f"  adsbdb: could not write unresolved log: {exc}")

    # -----------------------------------------------------------------
    # Apply
    # -----------------------------------------------------------------

    def _apply(self, aircraft: Aircraft, data: dict) -> None:
        """Apply a merged payload. Either half may be absent — an aircraft with
        a route but no airframe record is a normal case, not an edge one."""
        aircraft.raw.adsbdb = data

        ac = data.get("aircraft") or {}
        if aircraft.airframe.manufacturer is None and ac.get("manufacturer"):
            aircraft.airframe.manufacturer = ac["manufacturer"]
        if aircraft.airframe.registration is None and ac.get("registration"):
            aircraft.airframe.registration = ac["registration"]
        # The only unguarded write in this method, and deliberately so: adsbdb's
        # description ("737MAX 8 200") is preferred over tar1090's ("BOEING
        # 737-800 MAX"). It is safe to prefer because it can no longer destroy
        # the ICAO designator — that lives in type_code, which this never writes.
        if ac.get("type"):
            aircraft.airframe.type_description = ac["type"]
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
        if aircraft.route.origin_municipality is None and origin.get("municipality"):
            aircraft.route.origin_municipality = origin["municipality"]
        if aircraft.route.origin_country is None and origin.get("country_name"):
            aircraft.route.origin_country = origin["country_name"]
        if aircraft.route.destination_iata is None and dest.get("iata_code"):
            aircraft.route.destination_iata = dest["iata_code"]
        if aircraft.route.destination_name is None and dest.get("name"):
            aircraft.route.destination_name = dest["name"]
        if aircraft.route.destination_municipality is None and dest.get("municipality"):
            aircraft.route.destination_municipality = dest["municipality"]
        if aircraft.route.destination_country is None and dest.get("country_name"):
            aircraft.route.destination_country = dest["country_name"]


KEYS = {"log_unresolved"}


def get(cfg: dict, ctx: ModuleContext) -> AdsbdbEnricher:
    cache_dir = ctx.module_dir
    cache_dir.mkdir(parents=True, exist_ok=True)
    return AdsbdbEnricher(
        cache_dir      = cache_dir,
        log_unresolved = bool(cfg.get("log_unresolved", False)),
    )
