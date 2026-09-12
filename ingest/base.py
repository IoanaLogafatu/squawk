"""
ingest/base.py

BaseIngest — the interface every ingest module implements.

An ingest module answers one question: what can you see right now? It is
called once per loop of its chain runner and returns a list of aircraft,
even when the source only ever produces one. The runner owns the loop, the
sleeping, the transforms and the write to the snapshot; the module owns
nothing but the observation.

Modules are instantiated once and polled repeatedly, so they may keep
state on themselves between polls — a session, a cache, a last-seen
cursor. Nothing in the runner resets them.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from schemas.aircraft import Aircraft

if TYPE_CHECKING:
    from config import Config, IngestChainConfig


class BaseIngest(ABC):

    def __init__(self, name: str, chain: "IngestChainConfig", app: "Config") -> None:
        self.name  = name
        self.chain = chain
        self.app   = app
        self.log   = logging.getLogger(chain.name)

    @abstractmethod
    def poll(self) -> list[Aircraft]:
        """
        Everything visible now, as a list.

        Return an empty list when nothing is visible — that is a normal
        answer, not an error, and the snapshot's expiry loop is what turns
        a sustained empty answer into aircraft disappearing.
        """
        raise NotImplementedError
