"""
chains/output_chain.py

Runner for any [output_chain.<name>] block.

An output chain has exactly one input, chosen by its `source`:

    live       read the snapshot -> run the chain's transforms -> output.send()
    deletions  read new deletion notices -> run the chain's transforms
                                         -> output.on_deleted(), once per aircraft

One OS process per configured block, each on its own poll interval. It
reads files directly; nothing pushes to it, and it holds no connection to
any other process.

A destination that cares about both — the console's display and history,
or a push notifier that wants arrivals and departures — is two chain
blocks of the same module type, one per source. Each process then only
ever handles one kind of data, so nothing can be sent twice.

A deletions chain never reads the live snapshot. It keeps its own cursor
into the snapshot's deletion notices, on disk at
<data_dir>/output/<name>/deletions_cursor.txt, so every such chain sees
every deletion exactly once, in order, regardless of how fast it polls —
and a restart picks up where it left off rather than replaying.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from chains.runner import build_snapshot, sleep_interruptibly
from config import Config, OutputChainConfig
from output import OUTPUTS
from output.base import BaseOutput
from snapshot.base import BaseSnapshot
from transforms import apply_transforms, build_transforms
from transforms.base import BaseTransform


def run(chain: OutputChainConfig, app: Config) -> None:
    log        = logging.getLogger(chain.name)
    output     = OUTPUTS[chain.type](chain.type, chain, app)
    transforms = build_transforms(chain, app)
    snapshot   = build_snapshot(app)
    cursor     = CursorFile(app.data_dir, chain.name) if chain.source == "deletions" else None

    log.info("output chain '%s' started (%s, source=%s, polling every %.0fs)",
             chain.name, chain.type, chain.source, chain.poll_interval_seconds)

    while True:
        sleep_interruptibly(chain.poll_interval_seconds)

        if cursor is None:
            poll_live(snapshot, transforms, output)
        else:
            poll_deletions(snapshot, transforms, output, cursor, log)


def poll_live(snapshot: BaseSnapshot, transforms: list[BaseTransform], output: BaseOutput) -> None:
    """One pass of a live chain: the whole current picture, to send()."""
    aircraft = snapshot.read_all()
    aircraft = apply_transforms(transforms, aircraft)
    output.send(aircraft)


def poll_deletions(
    snapshot:   BaseSnapshot,
    transforms: list[BaseTransform],
    output:     BaseOutput,
    cursor:     "CursorFile",
    log:        logging.Logger,
) -> None:
    """
    One pass of a deletions chain: each newly expired aircraft, to on_deleted().

    on_deleted() is called once per aircraft, each call still carrying a
    list — one aircraft long — like every other module hand-off.
    """
    deleted, position = snapshot.read_deletions_since(cursor.read())
    if deleted:
        deleted = apply_transforms(transforms, deleted)
        log.debug("%d deletion(s) to report", len(deleted))
        for aircraft in deleted:
            output.on_deleted([aircraft])
    if position is not None:
        cursor.write(position)


class CursorFile:
    """One output chain's place in the deletion notices."""

    def __init__(self, data_dir: Path, chain_name: str) -> None:
        self.path = Path(data_dir) / "output" / chain_name / "deletions_cursor.txt"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._cached: str | None = None
        self._loaded = False

    def read(self) -> str | None:
        if not self._loaded:
            try:
                self._cached = self.path.read_text().strip() or None
            except (FileNotFoundError, OSError):
                self._cached = None
            self._loaded = True
        return self._cached

    def write(self, position: str) -> None:
        if position == self._cached:
            return
        tmp = self.path.with_suffix(".txt.tmp")
        tmp.write_text(position)
        os.replace(tmp, self.path)
        self._cached = position
        self._loaded = True
