"""
plugins/__init__.py

BasePlugin interface and factory for filter/enricher plugins.

Each plugin receives list[Aircraft] and returns list[Aircraft].
Filters reduce the list. Enrichers add data to each Aircraft.
Display plugins have their own factory in display/__init__.py.

Adding a plugin:
    1. Create plugins/<name>.py or plugins/<name>/ implementing BasePlugin
    2. Expose a get(cfg) factory function
    3. Reference it by name in config.toml under processor.plugins
"""

from __future__ import annotations

import importlib

from abc import ABC, abstractmethod

from schemas.aircraft import Aircraft


class BasePlugin(ABC):

    @abstractmethod
    def process(self, aircraft: list[Aircraft]) -> list[Aircraft]: ...


def get_plugin(name: str, cfg: dict | None = None) -> BasePlugin:
    cfg = cfg or {}
    try:
        module = importlib.import_module(f"plugins.{name}")
    except ModuleNotFoundError:
        raise ValueError(f"Unknown plugin: {name!r}")
    return module.get(cfg)
