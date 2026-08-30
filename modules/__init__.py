"""
modules/__init__.py

BaseModule interface and factory for filter/enricher modules.

Each module receives list[Aircraft] and returns list[Aircraft].
Filters reduce the list. Enrichers add data to each Aircraft.
Display modules have their own factory in display/__init__.py.

get_module() pools instances by (name, cfg) — one [modules.<name>] block always
resolves to one object, however many chains reference it. This is what lets a
module hold a connection, cache, or rate limiter without hand-rolling its own
singleton: instances are shared across chains that run in separate threads, so
mutable state must be guarded (see docs/modules-guide.md).

Adding a module:
    1. Create modules/<name>.py or modules/<name>/ implementing BaseModule
    2. Expose a get(cfg) factory function
    3. Reference it by name in config.toml under processor.modules
"""

from __future__ import annotations

import importlib
import json
import threading

from abc import ABC, abstractmethod

from schemas.aircraft import Aircraft


class BaseModule(ABC):

    @abstractmethod
    def process(self, aircraft: list[Aircraft]) -> list[Aircraft]: ...


_INSTANCES: dict[tuple[str, str], BaseModule] = {}
_INSTANCES_LOCK = threading.Lock()


def _cfg_key(cfg: dict) -> str:
    """Stable, hashable representation of a module's config block."""
    return json.dumps(cfg, sort_keys=True, default=str)


def get_module(name: str, cfg: dict | None = None) -> BaseModule:
    cfg = cfg or {}
    key = (name, _cfg_key(cfg))
    with _INSTANCES_LOCK:
        if key not in _INSTANCES:
            module_type = cfg.get("type", cfg.get("module", name))
            try:
                module = importlib.import_module(f"modules.{module_type}")
            except ModuleNotFoundError:
                raise ValueError(f"Unknown module: {name!r} (resolved to {module_type!r})")

            keys = getattr(module, "KEYS", None)
            if keys is not None:
                unknown = set(cfg) - keys
                if unknown:
                    print(f"  config: [modules.{name}] has unrecognised key(s): "
                          f"{', '.join(sorted(unknown))}")

            _INSTANCES[key] = module.get(cfg)
        return _INSTANCES[key]


def clear_module_pool() -> None:
    """Drop all pooled instances. For tests only."""
    with _INSTANCES_LOCK:
        _INSTANCES.clear()

