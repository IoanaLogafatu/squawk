"""
ingestor/__init__.py

Ingestor package. Each ingestor lives in its own sub-package and exposes
a run() function as its entry point.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from modules import BaseModule


def get_ingest_modules(cfg: dict) -> list[BaseModule]:
    """Build the modules named in an ingestor's config block.

    These run against every aircraft before it enters storage, so they
    define what the whole installation can see — see docs/modules-guide.md.
    """
    from config import config
    from modules import get_module

    return [
        get_module(name, config.modules.get(name, {}))
        for name in cfg.get("modules", [])
    ]
