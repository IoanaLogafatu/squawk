"""
ingestor/personal_adsb/ingestor.py

PersonalADSB ingestor — polls one or more readsb/tar1090 receivers,
merges their snapshots (most recently observed per ICAO hex wins), converts
each aircraft record into the Squawk schema, and writes the resulting
Aircraft objects to storage.

This ingestor represents a single logical source — a personal ADS-B
installation, potentially with multiple antennas/receivers for all-round
coverage. The merge presents one unified view keyed by observed_at.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import requests

from config import config
from ingestor.personal_adsb.converter import convert_aircraft
from schemas.aircraft import Aircraft

if TYPE_CHECKING:
    from modules import BaseModule
    from storage import BaseStorage

SOURCE_NAME = "PersonalADSB"


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def _fetch_snapshot(url: str, timeout: int) -> tuple[dict | None, str | None]:
    """
    Fetch one receiver's aircraft.json.
    Returns (snapshot, None) on success, (None, error_message) on failure.
    Network errors are swallowed — one dead receiver should not kill the loop.
    """
    try:
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
        return response.json(), None
    except (requests.RequestException, ValueError) as err:
        return None, str(err)


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

def _merge_snapshots(snapshots: list[tuple[str, dict]]) -> list[tuple[dict, datetime]]:
    """
    Merge aircraft records from multiple receiver snapshots.

    For each unique ICAO hex, keeps the most recently observed record —
    the one with the largest observed_at (computed as snapshot.now - seen_seconds).

    Args:
        snapshots: List of (receiver_name, raw_snapshot) tuples.

    Returns:
        List of (raw_aircraft_record, observed_at) tuples, ready for conversion.
    """
    # icao_hex -> (observed_at, raw_record)
    merged: dict[str, tuple[datetime, dict]] = {}

    for _receiver_name, snapshot in snapshots:
        snapshot_now = snapshot.get("now", time.time())
        for raw_aircraft in snapshot.get("aircraft", []):

            hex_id = raw_aircraft.get("hex", "").upper()
            if not hex_id:
                continue  # malformed, skip

            seen        = raw_aircraft.get("seen", 9999.0)
            observed_at = datetime.fromtimestamp(snapshot_now - seen, tz=timezone.utc)
            existing    = merged.get(hex_id)

            if existing is None or observed_at > existing[0]:
                merged[hex_id] = (observed_at, raw_aircraft)

    return [
        (raw_record, observed_at)
        for observed_at, raw_record in merged.values()
    ]


# ---------------------------------------------------------------------------
# Build aircraft
# ---------------------------------------------------------------------------

def _build_aircraft(merged: list[tuple[dict, datetime]]) -> list[Aircraft]:
    """
    Convert merged raw records into Aircraft objects.
    Malformed records (no ICAO hex) are silently dropped by the converter.
    """
    aircraft: list[Aircraft] = []
    for raw_record, observed_at in merged:
        converted = convert_aircraft(raw_record, observed_at=observed_at)
        if converted is not None:
            aircraft.append(converted)
    return aircraft


# ---------------------------------------------------------------------------
# Carry forward stable enrichment
# ---------------------------------------------------------------------------

def _carry_forward_enrichment(aircraft: list[Aircraft], storage: "BaseStorage") -> list[Aircraft]:
    """Fill each freshly-built Aircraft's UNKNOWN airframe/route fields from
    the matching hex's record already in storage, so hex/callsign-keyed
    enrichment modules (tar1090_db, vrs_route) don't repeat a lookup they've
    already done for an aircraft still in range.

    storage.retrieve_aircraft(hex) already returns None for a hex that is
    absent *or* stale past STALE_SECONDS — that's the only staleness check
    needed here; STALE_SECONDS was already the right shape for "how long do
    we trust this aircraft's enrichment without redoing it", the same
    question it already answers for "when does an aircraft disappear from
    the display".

    Every enrichment module already guards its own writes with
    `if field is None`, so once this fills a field, that module's own
    lookup naturally sees it's already answered and skips itself — no
    change needed in tar1090_db.py or vrs_route.py.

    meta, location and direction are never touched — always this cycle's
    fresh raw data. Within route, callsign and squawk_code are excluded for
    the same reason: the converter already set them from this cycle's raw
    payload, not from enrichment.
    """
    for a in aircraft:
        existing = storage.retrieve_aircraft(a.meta.icao_hex)
        if existing is None:
            continue   # new sighting, or one old enough to treat as a fresh flight

        existing_af = existing.get("airframe") or {}
        af = a.airframe
        if af.registration is None:
            af.registration = existing_af.get("registration")
        if af.type_code is None:
            af.type_code = existing_af.get("type_code")
        if af.type_description is None:
            af.type_description = existing_af.get("type_description")
        if af.category is None:
            af.category = existing_af.get("category")
        if af.db_flags is None:
            af.db_flags = existing_af.get("db_flags")
        if af.manufacturer is None:
            af.manufacturer = existing_af.get("manufacturer")
        if af.operator is None:
            af.operator = existing_af.get("operator")

        existing_rt = existing.get("route") or {}
        # Keyed by callsign, not just hex: if the callsign has changed since
        # the stored record was written, the aircraft has moved on to a
        # different flight and the old route data must not carry over. Both
        # sides None (no callsign transmitted either cycle) counts as a
        # match — nothing distinguishes flights by, safe to carry forward.
        if a.route.callsign != existing_rt.get("callsign"):
            continue

        rt = a.route
        if rt.origin_iata is None:
            rt.origin_iata = existing_rt.get("origin_iata")
        if rt.origin_name is None:
            rt.origin_name = existing_rt.get("origin_name")
        if rt.origin_municipality is None:
            rt.origin_municipality = existing_rt.get("origin_municipality")
        if rt.origin_country is None:
            rt.origin_country = existing_rt.get("origin_country")
        if rt.destination_iata is None:
            rt.destination_iata = existing_rt.get("destination_iata")
        if rt.destination_name is None:
            rt.destination_name = existing_rt.get("destination_name")
        if rt.destination_municipality is None:
            rt.destination_municipality = existing_rt.get("destination_municipality")
        if rt.destination_country is None:
            rt.destination_country = existing_rt.get("destination_country")
        if rt.flight_number is None:
            rt.flight_number = existing_rt.get("flight_number")
        if rt.airline_name is None:
            rt.airline_name = existing_rt.get("airline_name")
        if rt.airline_country is None:
            rt.airline_country = existing_rt.get("airline_country")

    return aircraft


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run(ingest_modules: list["BaseModule"]) -> None:
    """
    Main poll loop. Runs until interrupted.
    Reads configuration from config and writes Aircraft records to storage.
    """
    cfg = config.ingestors.get("personal_adsb", {})

    if not cfg.get("enabled", False):
        print(f"{SOURCE_NAME}: disabled in config, exiting.")
        return

    from storage import get_storage
    storage = get_storage(config.storage.backend, config.squawk.data_dir)

    last_seen: dict[str, datetime] = {}  # persists across poll cycles
    healthy:   dict[str, bool]     = {}  # persists across poll cycles

    while True:
        cycle_start = time.time()

        # Fetch from all receivers, logging health transitions only
        snapshots: list[tuple[str, dict]] = []

        for receiver in cfg.get("receivers", []):
            name = receiver["name"]
            snapshot, err = _fetch_snapshot(receiver["url"], timeout=cfg.get("timeout_seconds", 3))
            if snapshot is not None:
                if healthy.get(name) is False:
                    print(f"{SOURCE_NAME}: {name} recovered")
                healthy[name] = True
                last_seen[name] = datetime.now(timezone.utc)
                snapshots.append((name, snapshot))
            else:
                if healthy.get(name) is not False:
                    print(f"{SOURCE_NAME}: {name} unreachable — {err}")
                healthy[name] = False

        if snapshots:
            merged   = _merge_snapshots(snapshots)
            aircraft = _build_aircraft(merged)
            aircraft = _carry_forward_enrichment(aircraft, storage)

            for m in ingest_modules:
                aircraft = m.process(aircraft)

            storage.save_aircraft_array(aircraft)


        sleep_for = max(0.0, cfg.get("poll_interval_seconds", 5) - (time.time() - cycle_start))
        time.sleep(sleep_for)


if __name__ == "__main__":
    from ingestor import get_ingest_modules
    try:
        run(get_ingest_modules(config.ingestors.get("personal_adsb", {})))
    except KeyboardInterrupt:
        print("\nStopped.")
