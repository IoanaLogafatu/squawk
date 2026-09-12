"""
snapshot/file_object.py

The snapshot as a folder of files — one JSON object per aircraft, named
for its ICAO hex.

There is no snapshot server and no snapshot protocol. "The snapshot" is
this folder plus the merge rule below, applied identically by every
process that writes. An ingest chain calls save() in its own process; an
output chain calls read_all() in its own process; neither needs the other
to be running, and nothing needs to have started first.

The merge rule, for an aircraft already in the snapshot:

  - Older than what is on disk? Discard it. Two ingest chains observing
    the same aircraft will regularly race, and the loser of the race is
    carrying a worse answer, not a newer one.
  - location, direction and raw always overwrite. A position is only
    meaningful as a complete set; filling its blanks from an older record
    would place the aircraft somewhere it has never been.
  - Every other field fills in only where the snapshot's value is still
    UNKNOWN. That is what lets one chain observe the aircraft and another
    enrich it without either clobbering the other's work.
  - last_seen always advances, being the thing the rule is measured on.

What makes it correct is that read-merge-write happens under a per-hex
lock, not merely that the write is atomic: two chains that both read the
old record and then both write would each produce a file that is
individually consistent and jointly wrong.

Layout under <data_dir>/snapshot/:
    <HEX>.json                       one aircraft
    _locks/<HEX>.lock                per-aircraft lock, see above
    _deletions/<stamp>-<HEX>.json    expiry notices for output chains
"""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import fields, is_dataclass
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from schemas.aircraft import Aircraft, ensure_utc, now_utc
from snapshot.base import BaseSnapshot

if TYPE_CHECKING:
    from config import Config, SnapshotChainConfig

# Sub-folders are underscore-prefixed and read_all() globs "*.json" at the
# top level only, so bookkeeping never turns up as an aircraft.
LOCKS_DIR     = "_locks"
DELETIONS_DIR = "_deletions"

# Sections replaced wholesale on merge rather than filled field by field.
OVERWRITE_SECTIONS = ("location", "direction", "raw")


class FileObjectSnapshot(BaseSnapshot):

    def __init__(self, name: str, chain: "SnapshotChainConfig", app: "Config") -> None:
        super().__init__(name, chain, app)
        self.dir           = Path(app.data_dir) / "snapshot"
        self.locks_dir     = self.dir / LOCKS_DIR
        self.deletions_dir = self.dir / DELETIONS_DIR
        for directory in (self.dir, self.locks_dir, self.deletions_dir):
            directory.mkdir(parents=True, exist_ok=True)

    # -- writing ------------------------------------------------------------

    def save(self, aircraft: list[Aircraft]) -> None:
        """
        Merge a batch of observations into the snapshot.

        Called in-process by whichever ingest chain produced the batch.
        """
        aircraft = self.apply_own_transforms(aircraft)

        for incoming in aircraft:
            hex_code = _hex_of(incoming)
            if hex_code is None:
                self.log.warning("discarding an aircraft with no icao_hex — it has no key")
                continue

            with self._lock(hex_code):
                existing = self._read_one(hex_code)

                if existing is None:
                    self._write_one(hex_code, incoming)
                    self.log.debug("%s: new aircraft", hex_code)
                    continue

                if not _is_newer(incoming, existing):
                    self.log.debug(
                        "%s: discarding %s observation, not newer than what we hold",
                        hex_code, incoming.meta.ingest_source)
                    continue

                merged = merge(existing, incoming)
                self._write_one(hex_code, merged)
                self.log.debug("%s: merged observation from %s",
                               hex_code, incoming.meta.ingest_source)

    # -- reading ------------------------------------------------------------

    def read_all(self) -> list[Aircraft]:
        """
        Every aircraft in the snapshot.

        Called in-process by whichever output chain wants the picture. A
        file that fails to parse is skipped with a warning rather than
        taking the whole poll down: a half-written file cannot happen (all
        writes are atomic renames) but a hand-edited one can.
        """
        aircraft = []
        for path in sorted(self.dir.glob("*.json")):
            loaded = self._load(path)
            if loaded is not None:
                aircraft.append(loaded)
        return aircraft

    # -- expiry -------------------------------------------------------------

    def expire(self) -> list[Aircraft]:
        """
        Delete aircraft unseen for longer than expiry_minutes.

        Runs under the same per-hex lock as save(), so an aircraft cannot
        be deleted in the gap between an ingest chain reading it and
        writing its merge back.
        """
        cutoff  = now_utc() - timedelta(minutes=self.chain.expiry_minutes)
        deleted = []

        for path in sorted(self.dir.glob("*.json")):
            hex_code = path.stem
            with self._lock(hex_code):
                aircraft = self._load(path)
                if aircraft is None:
                    continue

                last_seen = aircraft.meta.last_seen
                if last_seen is not None and ensure_utc(last_seen) >= cutoff:
                    continue

                path.unlink(missing_ok=True)
                self._record_deletion(hex_code, aircraft)
                deleted.append(aircraft)
                self.log.info("%s: expired, last seen %s", hex_code, last_seen)

        return deleted

    def read_deletions_since(self, cursor: str | None) -> tuple[list[Aircraft], str | None]:
        """
        Deletion notices newer than the caller's cursor.

        Notice filenames start with a UTC timestamp, so sorting them by
        name sorts them by time and the cursor is simply the last name
        seen. No per-chain state lives in the snapshot itself.
        """
        names = sorted(p.name for p in self.deletions_dir.glob("*.json"))
        fresh = [n for n in names if cursor is None or n > cursor]

        aircraft = []
        for name in fresh:
            loaded = self._load(self.deletions_dir / name)
            if loaded is not None:
                aircraft.append(loaded)

        # Advance the cursor over every notice examined, including any that
        # failed to load — a bad file must not be retried forever.
        new_cursor = fresh[-1] if fresh else cursor
        return aircraft, new_cursor

    def prune_deletions(self) -> int:
        """
        Sweep up notices old enough that every output chain has had its
        chance to see them. Retention has to comfortably exceed the
        slowest output chain's poll interval or a slow chain misses one.
        """
        cutoff  = now_utc().timestamp() - self.chain.deletion_retention_minutes * 60
        removed = 0
        for path in self.deletions_dir.glob("*.json"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                continue
        return removed

    # -- files --------------------------------------------------------------

    def _path(self, hex_code: str) -> Path:
        return self.dir / f"{hex_code}.json"

    @contextmanager
    def _lock(self, hex_code: str) -> Iterator[None]:
        """Exclusive lock for one aircraft, held across read-merge-write."""
        lock_path = self.locks_dir / f"{hex_code}.lock"
        with open(lock_path, "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _read_one(self, hex_code: str) -> Aircraft | None:
        return self._load(self._path(hex_code))

    def _load(self, path: Path) -> Aircraft | None:
        try:
            with open(path) as handle:
                return Aircraft.from_json(handle.read())
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, ValueError, TypeError, OSError) as exc:
            self.log.warning("skipping unreadable snapshot file %s (%s)", path, exc)
            return None

    def _write_one(self, hex_code: str, aircraft: Aircraft) -> None:
        _atomic_write(self._path(hex_code), aircraft.to_json())

    def _record_deletion(self, hex_code: str, aircraft: Aircraft) -> None:
        """
        Leave a notice that an aircraft has gone.

        The timestamp is the deletion time, not last_seen, so notices sort
        in the order output chains should receive them.
        """
        stamp = now_utc().strftime("%Y%m%dT%H%M%S.%f")
        _atomic_write(self.deletions_dir / f"{stamp}-{hex_code}.json", aircraft.to_json())


# ---------------------------------------------------------------------------
# Merge — a free function, so it can be reasoned about and tested without a
# filesystem anywhere near it
# ---------------------------------------------------------------------------

def merge(existing: Aircraft, incoming: Aircraft) -> Aircraft:
    """
    Fold a newer observation into the record already held.

    Assumes the caller has established that incoming is newer; see
    _is_newer. Returns a new Aircraft — neither argument is modified.
    """
    merged = deepcopy(existing)

    for section in OVERWRITE_SECTIONS:
        setattr(merged, section, deepcopy(getattr(incoming, section)))

    _fill_blanks(merged.meta,     incoming.meta)
    _fill_blanks(merged.route,    incoming.route)
    _fill_blanks(merged.airframe, incoming.airframe)

    # last_seen is the one field the fill-if-blank rule must not apply to:
    # it is never blank on a stored record, and if it never advanced the
    # snapshot would freeze at its first observation and expire mid-flight.
    if incoming.meta.last_seen is not None:
        merged.meta.last_seen = incoming.meta.last_seen

    return merged


def _fill_blanks(target: object, source: object) -> None:
    """
    Copy source values into target wherever target is still UNKNOWN.

    Recurses into nested schema objects (Route.origin, Route.airline), so
    a transform that learns only the destination's country fills that one
    leaf and leaves the rest of the record alone.
    """
    for f in fields(target):  # type: ignore[arg-type]
        current  = getattr(target, f.name)
        incoming = getattr(source, f.name)

        if is_dataclass(current) and is_dataclass(incoming):
            _fill_blanks(current, incoming)
        elif current is None and incoming is not None:
            setattr(target, f.name, incoming)


def _is_newer(incoming: Aircraft, existing: Aircraft) -> bool:
    """
    Whether an incoming observation supersedes the one held.

    An incoming record with no last_seen cannot be placed in time, so it
    is never treated as newer. A stored record with no last_seen has
    nothing to compare against, so anything supersedes it.
    """
    if incoming.meta.last_seen is None:
        return False
    if existing.meta.last_seen is None:
        return True
    return ensure_utc(incoming.meta.last_seen) > ensure_utc(existing.meta.last_seen)


def _hex_of(aircraft: Aircraft) -> str | None:
    """The snapshot key, normalised — the filename is the identity."""
    hex_code = aircraft.meta.icao_hex
    if hex_code is None:
        return None
    hex_code = hex_code.strip().upper()
    return hex_code or None


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temp file and rename, so no reader ever sees a partial file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
