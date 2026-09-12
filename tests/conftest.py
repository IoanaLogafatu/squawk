"""
tests/conftest.py

Shared fixtures.

Every test gets its own data_dir under tmp_path, so nothing here ever
touches a real installation's snapshot and tests cannot interfere with
each other through the filesystem — which, given that the filesystem is
the entire IPC mechanism, is the whole ballgame.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Config, IngestChainConfig, OutputChainConfig, SnapshotChainConfig  # noqa: E402
from schemas.aircraft import Aircraft, Airframe, Meta, Route, now_utc  # noqa: E402
from snapshot.file_object import FileObjectSnapshot  # noqa: E402


@pytest.fixture
def app(tmp_path: Path) -> Config:
    """A minimal but valid installation config pointing at a temp data_dir."""
    return Config(
        data_dir           = tmp_path / "data",
        observer_latitude  = 52.0,
        observer_longitude = -1.0,
        services           = ["concorde"],
        snapshot_chain     = SnapshotChainConfig(
            name="snapshot_chain", type="file_object", transform=["nop"],
            expiry_minutes=5, scan_interval_seconds=30,
        ),
        ingest_chains      = {
            "concorde_A": IngestChainConfig(
                name="concorde_A", type="ingest_concorde", transform=["nop"]),
        },
        output_chains      = {
            "display": OutputChainConfig(
                name="display", type="output_console", transform=["nop"]),
        },
    )


@pytest.fixture
def snap(app: Config) -> FileObjectSnapshot:
    return FileObjectSnapshot("file_object", app.snapshot_chain, app)


@pytest.fixture
def make_aircraft():
    """
    Build an aircraft with a controllable last_seen.

    Age is expressed in seconds before now, so a test reads as "this
    observation is ten seconds older than that one" rather than as
    datetime arithmetic.
    """
    def _make(
        hex_code: str = "400F6A",
        age_seconds: float = 0.0,
        source: str = "concorde_A",
        **fields,
    ) -> Aircraft:
        aircraft = Aircraft(
            meta=Meta(
                icao_hex      = hex_code,
                ingest_source = source,
                last_seen     = now_utc() - timedelta(seconds=age_seconds),
            ),
            route=Route(callsign="BAW002"),
            airframe=Airframe(registration="G-BOAC"),
        )
        for key, value in fields.items():
            setattr(aircraft, key, value)
        return aircraft

    return _make
