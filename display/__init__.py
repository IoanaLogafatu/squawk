"""
display/__init__.py

Display module interface and factory.

Each display receives list[Aircraft], emits output (screen, file, network),
and returns the list unchanged.

Adding a display:
    1. Create display/<name>/ implementing BaseModule
    2. Expose a get(cfg, ctx) factory function
    3. Reference it by name in config.toml under processor.display
"""

from __future__ import annotations

import importlib
from pathlib import Path

from modules import BaseModule, ModuleContext


def get_display(name: str, cfg: dict | None = None) -> BaseModule:
    cfg = cfg or {}
    try:
        module = importlib.import_module(f"display.{name}")
    except ModuleNotFoundError:
        raise ValueError(f"Unknown display: {name!r}")

    from config import config as squawk_config
    data_dir = Path(squawk_config.squawk.data_dir)
    ctx = ModuleContext(
        data_dir   = data_dir,
        module_dir = data_dir / "display" / name,
        observer   = squawk_config.observer,
    )
    return module.get(cfg, ctx)
