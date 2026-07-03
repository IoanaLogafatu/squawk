"""
modules/__init__.py

BaseModule interface and factory for filter/enricher modules.

Each module receives list[Aircraft] and returns list[Aircraft].
Filters reduce the list. Enrichers add data to each Aircraft.
Display modules have their own factory in display/__init__.py.

Adding a module:
    1. Create modules/<name>.py or modules/<name>/ implementing BaseModule
    2. Expose a get(cfg) factory function
    3. Reference it by name in config.toml under processor.modules
"""

from __future__ import annotations

import importlib

from abc import ABC, abstractmethod

from schemas.aircraft import Aircraft


class BaseModule(ABC):

    @abstractmethod
    def process(self, aircraft: list[Aircraft]) -> list[Aircraft]: ...


def get_module(name: str, cfg: dict | None = None) -> BaseModule:
    cfg = cfg or {}
    try:
        module = importlib.import_module(f"modules.{name}")
    except ModuleNotFoundError:
        raise ValueError(f"Unknown module: {name!r}")
    return module.get(cfg)
