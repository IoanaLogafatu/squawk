"""
services/__init__.py

Registry of services.

A service named in config.toml's `services` list must appear here. Adding
one means writing the class and adding one line.
"""

from __future__ import annotations

from services.base import BaseService
from services.concorde.service import ConcordeService

SERVICES: dict[str, type[BaseService]] = {
    "concorde": ConcordeService,
}
