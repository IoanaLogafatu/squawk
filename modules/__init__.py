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

Each module's get(cfg, ctx) factory also receives a ModuleContext, built once
per instance from global config — a module should use ctx rather than reaching
for `from config import config` itself (see docs/modules-guide.md).

A module needing a dataset that other modules also read names it with
source = "<name>" and resolves it with ctx.data_source("<name>"), which returns
the one shared instance of that [data_sources.<name>] block rather than a
private download of its own (see docs/data-sources-guide.md).

Adding a module:
    1. Create modules/<name>.py or modules/<name>/ implementing BaseModule
    2. Expose a get(cfg, ctx) factory function
    3. Reference it by name in config.toml under processor.modules
"""

from __future__ import annotations

import importlib
import json
import threading

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from schemas.aircraft import Aircraft

if TYPE_CHECKING:
    from config import ObserverConfig
    from data_sources import BaseDataSource


class BaseModule(ABC):

    @abstractmethod
    def process(self, aircraft: list[Aircraft]) -> list[Aircraft]: ...


def _resolve_data_source(name: str) -> "BaseDataSource":
    """Resolve a [data_sources.<name>] block to its pooled instance.

    The default ModuleContext.data_source, supplied by the factory. Config has
    already checked that every module naming a source has a matching block, so
    a miss here means the context outlived the config that built it.
    """
    from config import config as squawk_config
    from data_sources import get_data_source

    source = squawk_config.data_sources.get(name)
    if source is None:
        raise ValueError(f"Unknown data source: {name!r} — no [data_sources.{name}] block")
    return get_data_source(source.name, source.cfg)


@dataclass(frozen=True)
class ModuleContext:
    """Everything a module may need from the wider installation.

    Built by the factory, one per module. Modules that need none of these
    accept it and ignore it — the signature is uniform.
    """
    data_dir:    Path              # installation data directory
    module_dir:  Path              # this module's own directory; not created
    observer:    "ObserverConfig"  # receiver position
    # Resolves a [data_sources.<name>] block to its shared instance. A module
    # naming source = "x" calls ctx.data_source("x") in its own get(); one with
    # no 'source' key never calls it. See docs/data-sources-guide.md.
    data_source: Callable[[str], "BaseDataSource"] = _resolve_data_source


# Recognised on every module block whatever its type, so no module has to list
# them in its own KEYS: 'type' names the implementation, 'source' names a
# [data_sources.<name>] block the module shares with other modules.
_COMMON_KEYS = {"type", "source"}

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
                unknown = set(cfg) - keys - _COMMON_KEYS
                if unknown:
                    print(f"  config: [modules.{name}] has unrecognised key(s): "
                          f"{', '.join(sorted(unknown))}")

            from config import config as squawk_config
            data_dir = Path(squawk_config.squawk.data_dir)
            ctx = ModuleContext(
                data_dir   = data_dir,
                module_dir = data_dir / "modules" / module_type,
                observer   = squawk_config.observer,
            )
            _INSTANCES[key] = module.get(cfg, ctx)
        return _INSTANCES[key]


def clear_module_pool() -> None:
    """Drop all pooled instances. For tests only."""
    with _INSTANCES_LOCK:
        _INSTANCES.clear()
