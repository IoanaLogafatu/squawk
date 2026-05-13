"""
storage/disk_drive.py

Disk-based storage backend. Saves each aircraft as an individual JSON file
under data_dir/tracked_aircraft/ and maintains a live view via retrieve_aircraft_array().

Writes are upsert-if-newer: a record is only written if its meta.observed_at
is more recent than the on-disk record for the same hex. After each write
cycle, records not updated within STALE_SECONDS are deleted.

retrieve_aircraft_array() also filters by staleness, so the processor always
sees only current aircraft regardless of when cleanup last ran.

Atomic writes (temp-and-replace) prevent partial reads.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from schemas.encoder import SquawkEncoder
from storage import BaseStorage, STALE_SECONDS
from schemas.aircraft import Aircraft


class DiskDriveStorage(BaseStorage):

    def __init__(self, data_dir: Path) -> None:
        self.aircraft_dir = data_dir / "tracked_aircraft"
        self.aircraft_dir.mkdir(parents=True, exist_ok=True)

    def save_aircraft_array(self, aircraft: list[Aircraft]) -> None:
        """Upsert each aircraft, then expire stale records."""
        for a in aircraft:
            path = self.aircraft_dir / f"{a.meta.icao_hex}.json"

            if a.meta.observed_at is not None and path.exists():
                try:
                    existing = json.loads(path.read_text())
                    existing_oa = existing.get("meta", {}).get("observed_at")
                    if existing_oa and a.meta.observed_at.isoformat() <= existing_oa:
                        continue  # on-disk record is same age or newer
                except (OSError, ValueError):
                    pass  # unreadable — fall through and overwrite

            tmp = path.with_suffix(".tmp")
            try:
                tmp.write_text(json.dumps(a, cls=SquawkEncoder, indent=2))
                tmp.replace(path)
            except OSError:
                pass  # silent; next cycle will retry

        self._expire_stale()

    def _expire_stale(self) -> None:
        """Delete aircraft files not updated within STALE_SECONDS."""
        now = time.time()
        for path in self.aircraft_dir.glob("*.json"):
            try:
                if now - path.stat().st_mtime > STALE_SECONDS:
                    path.unlink(missing_ok=True)
            except OSError:
                pass  # already deleted by a concurrent expiry — harmless

    def list_aircraft_hex_ids(self) -> list[str]:
        """Return sorted list of hex IDs currently on disk and not stale."""
        now = time.time()
        ids = []
        for p in self.aircraft_dir.glob("*.json"):
            try:
                if now - p.stat().st_mtime <= STALE_SECONDS:
                    ids.append(p.stem)
            except OSError:
                pass
        return sorted(ids)

    def retrieve_aircraft(self, hex_id: str) -> dict | None:
        """Read and return one aircraft record by hex ID, or None if absent or stale."""
        path = self.aircraft_dir / f"{hex_id}.json"
        try:
            if time.time() - path.stat().st_mtime > STALE_SECONDS:
                return None
            return json.loads(path.read_text())
        except FileNotFoundError:
            return None
        except ValueError:
            path.unlink(missing_ok=True)
            return None

    def retrieve_aircraft_array(self) -> list[dict]:
        """Return all non-stale aircraft as raw dicts."""
        result = []
        now = time.time()
        for path in self.aircraft_dir.glob("*.json"):
            try:
                if now - path.stat().st_mtime > STALE_SECONDS:
                    continue
                result.append(json.loads(path.read_text()))
            except (OSError, ValueError):
                pass
        return result


def get(data_dir: Path) -> DiskDriveStorage:
    return DiskDriveStorage(data_dir)
