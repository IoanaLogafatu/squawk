"""
chains/snapshot_chain.py

Runner for the single [snapshot_chain] block — the expiry loop.

This is the one part of the snapshot that needs a process. Writes and
reads happen inside the ingest and output processes, but nobody is
naturally responsible for noticing that an aircraft stopped being
observed, so one process wakes periodically and does exactly that:

    scan the snapshot -> delete anything past expiry_minutes
                      -> leave a deletion notice for the output chains
                      -> sweep up notices old enough that all have seen them

It does not write aircraft and it does not talk to any other process.
If it is not running, the snapshot simply never forgets anything.
"""

from __future__ import annotations

import logging

from chains.runner import build_snapshot, sleep_interruptibly
from config import Config, SnapshotChainConfig


def run(chain: SnapshotChainConfig, app: Config) -> None:
    log      = logging.getLogger(chain.name)
    snapshot = build_snapshot(app)

    log.info("snapshot chain started (%s, expiring after %.0f minutes, scanning every %.0fs)",
             chain.type, chain.expiry_minutes, chain.scan_interval_seconds)

    while True:
        sleep_interruptibly(chain.scan_interval_seconds)

        deleted = snapshot.expire()
        if deleted:
            log.info("expired %d aircraft: %s", len(deleted),
                     ", ".join(a.meta.icao_hex or "??????" for a in deleted))

        pruned = snapshot.prune_deletions()
        if pruned:
            log.debug("pruned %d old deletion notice(s)", pruned)
