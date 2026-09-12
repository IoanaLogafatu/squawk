"""
chains/runner.py

The handful of things all three chain runners need.

build_snapshot is here rather than in each runner because every family
touches the snapshot and they must all build it from the same block —
the snapshot chain's own configuration, including its transform list,
whichever process happens to be holding it.
"""

from __future__ import annotations

import time

from config import Config
from snapshot import SNAPSHOTS
from snapshot.base import BaseSnapshot


def build_snapshot(app: Config) -> BaseSnapshot:
    """The configured snapshot backend, built from the [snapshot_chain] block."""
    chain = app.snapshot_chain
    return SNAPSHOTS[chain.type](chain.type, chain, app)


def sleep_interruptibly(seconds: float) -> None:
    """
    Sleep, but wake promptly on Ctrl-C.

    A plain long sleep leaves a chain unresponsive to a keyboard
    interrupt for its whole interval, which is tiresome when you are
    running five of these in five terminals.
    """
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 0.5))
