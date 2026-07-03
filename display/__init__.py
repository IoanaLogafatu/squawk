"""
display/__init__.py

Display module interface and factory.

Each display receives list[Aircraft], emits output (screen, file, network),
and returns the list unchanged.

Adding a display:
    1. Create display/<name>/ implementing BaseModule
    2. Expose a get(cfg) factory function
    3. Reference it by name in config.toml under processor.display
"""

from __future__ import annotations

import importlib

from modules import BaseModule


def get_display(name: str, cfg: dict | None = None) -> BaseModule:
    cfg = cfg or {}
    try:
        module = importlib.import_module(f"display.{name}")
    except ModuleNotFoundError:
        raise ValueError(f"Unknown display: {name!r}")
    return module.get(cfg)
