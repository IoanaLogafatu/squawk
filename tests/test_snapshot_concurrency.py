"""
tests/test_snapshot_concurrency.py

The snapshot under concurrent writers.

save() is called in-process by every ingest chain there is, with no
coordination between them beyond a per-aircraft lock. If that lock only
covered the write and not the whole read-merge-write sequence, each
process would produce a file that was individually well-formed and
collectively missing whatever the other processes had learned — a lost
update, which is precisely the failure single-process testing cannot see.

Every writer stamps last_seen at the moment it writes, as the real
ingest chains do. That matters for the test's logic as much as its
realism: it means no write is ever stale on arrival, so a field that
fails to appear in the final record cannot be explained away as a
correctly-discarded old observation.

Real processes, not threads: an flock is between processes, and a
threaded test would pass with no flock at all.
"""

from __future__ import annotations

import multiprocessing as mp
from pathlib import Path

from config import Config, SnapshotChainConfig
from schemas.aircraft import Aircraft, Meta, ensure_utc, now_utc
from snapshot.file_object import FileObjectSnapshot

HEX = "400F6A"

# One fill-if-blank leaf per writer. Filling a blank never clears
# anything, so under correct locking every one of these survives into the
# final record no matter what order the writers ran in.
CLAIMS = [
    ("airframe", "registration",   "G-BOAC"),
    ("airframe", "type_code",      "CONC"),
    ("airframe", "manufacturer",   "BAC / Aerospatiale"),
    ("airframe", "operator",       "British Airways"),
    ("route",    "callsign",       "BAW002"),
    ("route",    "squawk_code",    "2346"),
    ("route",    "flight_number",  "BA002"),
    ("meta",     "reception_type", "adsb_icao"),
]

WRITES_EACH = 15


def _make_config(data_dir: Path) -> Config:
    return Config(
        data_dir           = Path(data_dir),
        observer_latitude  = 52.0,
        observer_longitude = -1.0,
        services           = ["concorde"],
        snapshot_chain     = SnapshotChainConfig(
            name="snapshot_chain", type="file_object", transform=["nop"]),
        ingest_chains      = {},
        output_chains      = {},
    )


def _hammer(args):
    """
    Child process: write WRITES_EACH observations, each claiming one field.

    Returns the newest timestamp it wrote, so the parent knows what the
    final record ought to be carrying.
    """
    data_dir, index, barrier = args
    app  = _make_config(data_dir)
    snap = FileObjectSnapshot("file_object", app.snapshot_chain, app)
    section, field, value = CLAIMS[index]

    barrier.wait()

    newest = None
    for _ in range(WRITES_EACH):
        stamp    = now_utc()
        aircraft = Aircraft(meta=Meta(
            icao_hex      = HEX,
            ingest_source = f"writer_{index}",
            last_seen     = stamp,
        ))
        setattr(getattr(aircraft, section), field, value)
        snap.save([aircraft])
        newest = stamp

    return newest


def _run_writers(data_dir: Path) -> list:
    workers = len(CLAIMS)
    with mp.Manager() as manager:
        barrier = manager.Barrier(workers)
        with mp.Pool(workers) as pool:
            return pool.map(_hammer,
                            [(str(data_dir), i, barrier) for i in range(workers)])


def test_concurrent_writers_do_not_lose_each_others_findings(tmp_path):
    data_dir = tmp_path / "data"

    _run_writers(data_dir)

    app  = _make_config(data_dir)
    held = FileObjectSnapshot("file_object", app.snapshot_chain, app).read_all()

    assert len(held) == 1
    record = held[0]

    missing = [
        f"{section}.{field}"
        for section, field, value in CLAIMS
        if getattr(getattr(record, section), field) != value
    ]
    assert not missing, f"lost update — these writers' findings vanished: {missing}"


def test_the_newest_observation_is_the_one_left_standing(tmp_path):
    data_dir = tmp_path / "data"

    reported = _run_writers(data_dir)

    app  = _make_config(data_dir)
    held = FileObjectSnapshot("file_object", app.snapshot_chain, app).read_all()[0]

    assert ensure_utc(held.meta.last_seen) == max(ensure_utc(t) for t in reported)


def test_concurrent_writers_leave_exactly_one_file(tmp_path):
    """No stray temp files, and one aircraft means one file."""
    data_dir = tmp_path / "data"

    _run_writers(data_dir)

    files = sorted(p.name for p in (data_dir / "snapshot").glob("*"))
    assert files == ["400F6A.json", "_deletions", "_locks"]
