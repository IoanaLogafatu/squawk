"""
snapshot/__init__.py

Registry of snapshot backends.

The backend decides how the shared picture is stored. Swapping it is one
line in config.toml plus one line here; nothing else in Squawk knows or
cares where aircraft live.
"""

from __future__ import annotations

from snapshot.base import BaseSnapshot
from snapshot.file_object import FileObjectSnapshot

SNAPSHOTS: dict[str, type[BaseSnapshot]] = {
    "file_object": FileObjectSnapshot,
}
