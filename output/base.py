"""
output/base.py

BaseOutput — the interface every output module implements.

An output module does something with aircraft: print them, render them,
push them, post them. Like every other module type it receives
list[Aircraft].

It has two hooks, and the chain's `source` decides which one is called —
never both from the same chain:

    send()        source = "live"       the whole current picture, every poll
    on_deleted()  source = "deletions"  one call per aircraft that expired

"Gone" is a separate hook rather than an absence in the next send()
because it is an event some outputs act on (a log, a notification) and
most never need. A module may implement either or both; a destination that
wants both is configured as two chains of the same module.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from schemas.aircraft import Aircraft

if TYPE_CHECKING:
    from config import Config, OutputChainConfig


class BaseOutput(ABC):

    def __init__(self, name: str, chain: "OutputChainConfig", app: "Config") -> None:
        self.name  = name
        self.chain = chain
        self.app   = app
        self.log   = logging.getLogger(chain.name)

    @abstractmethod
    def send(self, aircraft: list[Aircraft]) -> None:
        """Present the current picture. May be an empty list — that is a picture too."""
        raise NotImplementedError

    def on_deleted(self, aircraft: list[Aircraft]) -> None:
        """
        An aircraft has expired out of the snapshot.

        Called once per expired aircraft, with a list one aircraft long,
        and each deletion reaches each deletions chain exactly once.
        Default: ignore it.
        """
        return None
