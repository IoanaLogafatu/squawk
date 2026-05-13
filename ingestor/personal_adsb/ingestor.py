"""
ingestor/personal_adsb/ingestor.py

PersonalADSB ingestor — polls one or more readsb/tar1090 receivers,
merges their snapshots (most recently observed per ICAO hex wins), converts
each aircraft record into the Squawk schema, and emits a SquawkEnvelope
to all enabled repositories.

This ingestor represents a single logical source — a personal ADS-B
installation, potentially with multiple antennas/receivers for all-round
coverage. The envelope presents one unified view keyed by observed_at.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import requests

from config import config
from ingestor.personal_adsb.converter import convert_aircraft
from schemas.aircraft import Aircraft, ReceiverStatus, SquawkEnvelope

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
# Build envelope
# ---------------------------------------------------------------------------

def _build_envelope(
    merged: list[tuple[dict, datetime]],
    receiver_status: list[ReceiverStatus],
) -> SquawkEnvelope:
    """
    Convert merged raw records into a SquawkEnvelope.
    Malformed records (no ICAO hex) are silently dropped by the converter.
    """
    aircraft: list[Aircraft] = []

    for raw_record, observed_at in merged:
        converted = convert_aircraft(raw_record, observed_at=observed_at)
        if converted is not None:
            aircraft.append(converted)

    return SquawkEnvelope(
        source          = SOURCE_NAME,
        timestamp       = datetime.now(timezone.utc),
        aircraft_count  = len(aircraft),
        receiver_status = receiver_status,
        aircraft        = aircraft,
    )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run() -> None:
    """
    Main poll loop. Runs until interrupted.
    Reads configuration from config and dispatches each envelope
    to all enabled repositories.
    """
    cfg = config.ingestors.get("personal_adsb", {})

    if not cfg.get("enabled", False):
        print(f"{SOURCE_NAME}: disabled in config, exiting.")
        return

    from storage import get_storage
    storage = get_storage(config.storage.method, config.squawk.data_dir)

    last_seen: dict[str, datetime] = {}  # persists across poll cycles

    while True:
        cycle_start = time.time()

        # Fetch from all receivers, recording health for each
        snapshots:       list[tuple[str, dict]]  = []
        receiver_status: list[ReceiverStatus]    = []

        for receiver in cfg.get("receivers", []):
            snapshot, err = _fetch_snapshot(receiver["url"], timeout=cfg.get("timeout_seconds", 3))
            if snapshot is not None:
                now = datetime.now(timezone.utc)
                last_seen[receiver["name"]] = now
                receiver_status.append(ReceiverStatus(
                    name      = receiver["name"],
                    healthy   = True,
                    last_seen = now,
                ))
                snapshots.append((receiver["name"], snapshot))
            else:
                receiver_status.append(ReceiverStatus(
                    name      = receiver["name"],
                    healthy   = False,
                    last_seen = last_seen.get(receiver["name"]),
                    error     = err,
                ))

        if snapshots:
            merged   = _merge_snapshots(snapshots)
            envelope = _build_envelope(merged, receiver_status)
            storage.save_aircraft_array(envelope.aircraft)


        sleep_for = max(0.0, cfg.get("poll_interval_seconds", 5) - (time.time() - cycle_start))
        time.sleep(sleep_for)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print("\nStopped.")
