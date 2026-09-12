"""
output/base.py

BaseOutput — the interface every output module implements.

An output module is handed the current picture and does something with
it: print it, render it, push it, post it. It is called once per loop of
its chain runner, and like every other module type it receives
list[Aircraft].

Outputs also get told when an aircraft leaves. That is a separate hook
rather than an absence in the next send(), because "gone" is an event some
outputs act on (a log, a notification) and most simply ignore — so it has
a default implementation that does nothing, and only outputs that care
override it.
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
        Aircraft that have expired out of the snapshot since the last call.

        Delivered once per output chain. Default: ignore them.
        """
        return None
