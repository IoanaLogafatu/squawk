"""
chains/output_chain.py

Runner for any [output_chain.<name>] block.

    read the snapshot -> run the chain's transforms -> send to the output

One OS process per configured block, each on its own poll interval. It
reads the snapshot folder directly; nothing pushes to it, and it holds no
connection to any ingest process.

Deletions are delivered alongside the picture. Each output chain keeps its
own cursor into the snapshot's deletion notices, on disk at
<data_dir>/output/<name>/deletions_cursor.txt, so every chain sees every
deletion exactly once regardless of how fast it polls — and a restart
picks up where it left off rather than replaying or skipping.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from chains.runner import build_snapshot, sleep_interruptibly
from config import Config, OutputChainConfig
from output import OUTPUTS
from transforms import apply_transforms, build_transforms


def run(chain: OutputChainConfig, app: Config) -> None:
    log        = logging.getLogger(chain.name)
    output     = OUTPUTS[chain.type](chain.type, chain, app)
    transforms = build_transforms(chain, app)
    snapshot   = build_snapshot(app)
    cursor     = _CursorFile(app.data_dir, chain.name)

    log.info("output chain '%s' started (%s, polling every %.0fs)",
             chain.name, chain.type, chain.poll_interval_seconds)

    while True:
        sleep_interruptibly(chain.poll_interval_seconds)

        aircraft = snapshot.read_all()
        aircraft = apply_transforms(transforms, aircraft)
        output.send(aircraft)

        # Deletions bypass the transform list: they describe an aircraft
        # that is no longer in the picture, so a transform that filters or
        # enriches the picture has no say over them.
        deleted, position = snapshot.read_deletions_since(cursor.read())
        if deleted:
            log.debug("%d deletion(s) to report", len(deleted))
            output.on_deleted(deleted)
        if position is not None:
            cursor.write(position)


class _CursorFile:
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
