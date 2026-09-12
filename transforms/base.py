"""
transforms/base.py

BaseTransform — the interface every transform implements.

A transform takes a list of aircraft and returns a list of aircraft. That
is the whole contract, and it is deliberately the same in all three chain
families: the ingest chain, the snapshot chain and the output chain all
build their transform list the same way and call it the same way, so a
transform written for one works in any of them.

Transforms are instantiated once per chain and then called repeatedly, so
a transform is free to keep state on itself between calls — a cache, a
rate limiter, a connection. Nothing in the runners resets it.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from schemas.aircraft import Aircraft

if TYPE_CHECKING:
    from config import ChainConfig, Config


class BaseTransform(ABC):
    """
    Args:
        name:  this transform's registered type name, e.g. "nop".
        chain: the config block of the chain running it — a transform reads
               its own settings from there.
        app:   whole-installation config (data_dir, observer position).
    """

    def __init__(self, name: str, chain: "ChainConfig", app: "Config") -> None:
        self.name  = name
        self.chain = chain
        self.app   = app
        # Logger is named "<chain>.<transform>" so a debug line says which
        # chain's copy of a shared transform produced it.
        self.log = logging.getLogger(f"{chain.name}.{name}")

    @abstractmethod
    def process(self, aircraft: list[Aircraft]) -> list[Aircraft]:
        """Transform the list. May filter, enrich, reorder, or return as-is."""
        raise NotImplementedError
