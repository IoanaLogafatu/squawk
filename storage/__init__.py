"""
storage/__init__.py

Storage backend interface and factory.

Each backend implements BaseStorage. Add a new backend by creating
storage/<name>.py with a get(data_dir) factory function — nothing else
needs to change.
"""

from __future__ import annotations

import importlib
import threading
import time

from abc import ABC, abstractmethod
from pathlib import Path

from schemas.aircraft import Aircraft, aircraft_from_dict


STALE_SECONDS = 60  # Aircraft not updated within this window are considered gone
SNAPSHOT_TTL_SECONDS = 1.0


class BaseStorage(ABC):

    def __init__(self) -> None:
        self._snapshot_lock = threading.Lock()
        self._snapshot: list[Aircraft] = []
        self._snapshot_at = 0.0

    def retrieve_aircraft_objects(self) -> list[Aircraft]:
        """Deserialised snapshot, shared across callers within the TTL window."""
        now = time.monotonic()
        with self._snapshot_lock:
            if now - self._snapshot_at > SNAPSHOT_TTL_SECONDS:
                self._snapshot = [
                    aircraft_from_dict(d) for d in self.retrieve_aircraft_array()
                ]
                self._snapshot_at = now
            return list(self._snapshot)

    @abstractmethod
    def save_aircraft_array(self, aircraft: list[Aircraft]) -> None: ...

    @abstractmethod
    def list_aircraft_hex_ids(self) -> list[str]: ...

    @abstractmethod
    def retrieve_aircraft(self, hex_id: str) -> dict | None: ...

    @abstractmethod
    def retrieve_aircraft_array(self) -> list[dict]: ...


_INSTANCES: dict[tuple[str, str], BaseStorage] = {}
_INSTANCES_LOCK = threading.Lock()


def get_storage(backend: str, data_dir: Path) -> BaseStorage:
    key = (backend, str(data_dir))
    with _INSTANCES_LOCK:
        if key not in _INSTANCES:
            try:
                module = importlib.import_module(f"storage.{backend}")
            except ModuleNotFoundError:
                raise ValueError(f"Unknown storage backend: {backend!r}")
            _INSTANCES[key] = module.get(data_dir)
        return _INSTANCES[key]
