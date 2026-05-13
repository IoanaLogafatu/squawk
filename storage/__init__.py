"""
storage/__init__.py

Storage backend interface and factory.

Each backend implements BaseStorage. Add a new backend by creating
storage/<name>.py with a get(data_dir) factory function — nothing else
needs to change.
"""

from __future__ import annotations

import importlib

from abc import ABC, abstractmethod
from pathlib import Path

from schemas.aircraft import Aircraft


STALE_SECONDS = 60  # Aircraft not updated within this window are considered gone


class BaseStorage(ABC):

    @abstractmethod
    def save_aircraft_array(self, aircraft: list[Aircraft]) -> None: ...

    @abstractmethod
    def list_aircraft_hex_ids(self) -> list[str]: ...

    @abstractmethod
    def retrieve_aircraft(self, hex_id: str) -> dict | None: ...

    @abstractmethod
    def retrieve_aircraft_array(self) -> list[dict]: ...


def get_storage(method: str, data_dir: Path) -> BaseStorage:
    try:
        module = importlib.import_module(f"storage.{method}")
    except ModuleNotFoundError:
        raise ValueError(f"Unknown storage method: {method!r}")
    return module.get(data_dir)
