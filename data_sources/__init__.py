"""
data_sources/__init__.py

BaseDataSource interface and factory for shared external datasets.

A data source is a dataset that more than one module may need: one download,
one freshness check, one instance — however many modules name it. Deliberately
parallel to modules/, and separate from it for one reason: a source belongs to
no single module. Two modules reading the same dataset must not each own a
copy of it, and must not race each other to fetch it.

get_data_source() pools instances by (name, cfg), the same way get_module()
does — one [data_sources.<name>] block always resolves to one object, however
many modules reference it. That pooling is the whole point: it is what makes
"one download" true rather than merely intended.

A source's files live in <data_dir>/data_sources/<name>/, keyed on the block
*name* rather than the type (unlike a module's ctx.module_dir, which is keyed
on type). Two blocks of the same type are two different datasets — a different
region, a different feed — so they must not overwrite each other's files.

Refresh *policy* is each concrete type's own business; BaseDataSource only
fixes the contract that ensure_fresh() is cheap and safe to call often. The
two shapes already in view differ: "stale after N days, checked periodically"
(tar1090_db) and "published once daily at a known hour, have I got today's
yet" (VRS standing data). Forcing one policy onto both would be wrong.

Adding a source type:
    1. Create data_sources/<type>.py implementing BaseDataSource
    2. Expose a get(cfg, ctx) factory function
    3. Reference it by name in config.toml under [data_sources.<name>]

See docs/data-sources-guide.md.
"""

from __future__ import annotations

import importlib
import json
import threading

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


class BaseDataSource(ABC):

    @abstractmethod
    def ensure_fresh(self) -> None:
        """Check staleness and refresh if needed.

        Called by any module that uses this source, as often as that module
        likes — cheap to call repeatedly; the source itself decides how often
        a check actually does anything.
        """

    @property
    @abstractmethod
    def directory(self) -> Path:
        """Where this source's files live on disk."""


@dataclass(frozen=True)
class DataSourceContext:
    """Everything a source type may need from the wider installation.

    Not ModuleContext: only data_dir genuinely overlaps. `module_dir` would be
    a lie here (a source's directory is keyed on its own name, under
    data_sources/, not under modules/), and `observer` is a receiver position —
    meaningless to a dataset download.
    """
    data_dir:   Path   # installation data directory
    source_dir: Path   # this source's own directory; not created


# Recognised on every source block regardless of type, so a source type's own
# KEYS lists only its own options — same treatment `type` gets for modules.
_COMMON_KEYS = {"type"}

_INSTANCES: dict[tuple[str, str], BaseDataSource] = {}
_INSTANCES_LOCK = threading.Lock()


def _cfg_key(cfg: dict) -> str:
    """Stable, hashable representation of a source's config block."""
    return json.dumps(cfg, sort_keys=True, default=str)


def get_data_source(name: str, cfg: dict | None = None) -> BaseDataSource:
    cfg = cfg or {}
    key = (name, _cfg_key(cfg))
    with _INSTANCES_LOCK:
        if key not in _INSTANCES:
            source_type = cfg.get("type", name)
            try:
                module = importlib.import_module(f"data_sources.{source_type}")
            except ModuleNotFoundError:
                raise ValueError(f"Unknown data source: {name!r} (resolved to {source_type!r})")

            keys = getattr(module, "KEYS", None)
            if keys is not None:
                unknown = set(cfg) - keys - _COMMON_KEYS
                if unknown:
                    print(f"  config: [data_sources.{name}] has unrecognised key(s): "
                          f"{', '.join(sorted(unknown))}")

            from config import config as squawk_config
            data_dir = Path(squawk_config.squawk.data_dir)
            ctx = DataSourceContext(
                data_dir   = data_dir,
                source_dir = data_dir / "data_sources" / name,
            )
            _INSTANCES[key] = module.get(cfg, ctx)
        return _INSTANCES[key]


def clear_data_source_pool() -> None:
    """Drop all pooled instances. For tests only."""
    with _INSTANCES_LOCK:
        _INSTANCES.clear()
