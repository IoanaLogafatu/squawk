"""
snapshot/base.py

BaseSnapshot — the interface every snapshot backend implements.

The snapshot is the shared picture: ingest chains write into it, output
chains read from it, and neither of them talks to the other. It is not a
process. Whichever process is holding a snapshot module is the one doing
the work, in-process, at the moment it calls save() or read_all().

The snapshot chain owns a transform list of its own, applied to incoming
aircraft before they are merged. It is built here rather than by any
caller, because it belongs to the snapshot chain's configuration and not
to whoever happens to be writing.

Beyond the two data methods, a backend provides the expiry side: which
records have gone stale, and a record of what was removed so output
chains can be told. Expiry is the one part of the snapshot that does need
a process of its own — see chains/snapshot_chain.py.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from schemas.aircraft import Aircraft
from transforms import apply_transforms, build_transforms

if TYPE_CHECKING:
    from config import Config, SnapshotChainConfig


class BaseSnapshot(ABC):

    def __init__(self, name: str, chain: "SnapshotChainConfig", app: "Config") -> None:
        self.name       = name
        self.chain      = chain
        self.app        = app
        self.log        = logging.getLogger(chain.name)
        self.transforms = build_transforms(chain, app)

    # -- helpers ------------------------------------------------------------

    def apply_own_transforms(self, aircraft: list[Aircraft]) -> list[Aircraft]:
        """
        Run the snapshot chain's transforms over an incoming batch.

        Applied once to the batch, before any merge decision is made — not
        re-run per field, and not re-run for the merged result.
        """
        return apply_transforms(self.transforms, aircraft)

    # -- data ---------------------------------------------------------------

    @abstractmethod
    def save(self, aircraft: list[Aircraft]) -> None:
        """Merge a batch of observations into the snapshot."""
        raise NotImplementedError

    @abstractmethod
    def read_all(self) -> list[Aircraft]:
        """Every aircraft currently in the snapshot."""
        raise NotImplementedError

    # -- expiry -------------------------------------------------------------

    @abstractmethod
    def expire(self) -> list[Aircraft]:
        """Remove records older than the configured expiry. Returns what went."""
        raise NotImplementedError

    @abstractmethod
    def read_deletions_since(self, cursor: str | None) -> tuple[list[Aircraft], str | None]:
        """
        Deletions recorded since a caller's cursor.

        Returns the aircraft removed and a new cursor to pass next time.
        Each output chain keeps its own cursor, so a deletion is delivered
        to all of them exactly once each, independent of their poll rates.
        """
        raise NotImplementedError

    @abstractmethod
    def prune_deletions(self) -> int:
        """Discard deletion notices old enough that every chain has seen them."""
        raise NotImplementedError
