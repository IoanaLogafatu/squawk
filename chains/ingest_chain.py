"""
chains/ingest_chain.py

Runner for any [ingest_chain.<name>] block.

    poll the source -> run the chain's transforms -> save to the snapshot

One OS process per configured block. The two in the base build,
concorde_A and concorde_B, are deliberately identical: they observe the
same aircraft from the same service on independent, jittered schedules,
which is how the snapshot's merge and locking get exercised for real
rather than only in tests.

The snapshot module is built here and called directly, in-process. There
is nothing to connect to and no start-up order to respect — if the
snapshot folder does not exist yet, this process creates it.
"""

from __future__ import annotations

import logging
import random
import time

from chains.runner import build_snapshot, sleep_interruptibly
from config import Config, IngestChainConfig
from ingest import INGESTORS
from transforms import apply_transforms, build_transforms


def run(chain: IngestChainConfig, app: Config) -> None:
    log        = logging.getLogger(chain.name)
    ingestor   = INGESTORS[chain.type](chain.type, chain, app)
    transforms = build_transforms(chain, app)
    snapshot   = build_snapshot(app)

    log.info("ingest chain '%s' started (%s, polling every %.0f-%.0fs)",
             chain.name, chain.type, chain.poll_min_seconds, chain.poll_max_seconds)

    while True:
        # Sleep first, then poll: the jitter is what keeps two chains from
        # marching in step, and starting with it staggers them from the
        # very first poll rather than after the first collision.
        sleep_interruptibly(random.uniform(chain.poll_min_seconds, chain.poll_max_seconds))

        started  = time.monotonic()
        aircraft = ingestor.poll()
        aircraft = apply_transforms(transforms, aircraft)
        snapshot.save(aircraft)

        log.debug("polled %d aircraft in %.0fms",
                  len(aircraft), (time.monotonic() - started) * 1000)
