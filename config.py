"""
config.py

Loads and validates config.toml.

All other modules import from here — nothing reads the TOML file directly.
This gives one place to catch configuration errors before anything starts.

Usage:
    from config import config
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Config file location
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(__file__).parent / "config.toml"


# ---------------------------------------------------------------------------
# Typed config sections
# ---------------------------------------------------------------------------

@dataclass
class SquawkSystemConfig:
    data_dir: Path


@dataclass
class ObserverConfig:
    latitude:  float
    longitude: float


@dataclass
class StorageConfig:
    method: str   # e.g. "disk_drive"


@dataclass
class ProcessorConfig:
    poll_interval_seconds: int         # How often the processor runs
    modules:               list[str]   # Module names, applied in order
    display:               str | None  # Display module name


@dataclass
class SquawkConfig:
    squawk:    SquawkSystemConfig
    observer:  ObserverConfig
    storage:   StorageConfig
    ingestors: dict[str, dict]
    processor: ProcessorConfig | None = None
    display:   dict = field(default_factory=dict)   # Per-display config keyed by module name
    modules:   dict = field(default_factory=dict)   # Per-module config keyed by module name


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def _load_squawk(raw: dict) -> SquawkSystemConfig:
    squawk = raw.get("squawk", {})
    return SquawkSystemConfig(
        data_dir = Path(squawk.get("data_dir", "data")),
    )


def _load_observer(raw: dict) -> ObserverConfig:
    obs = raw["observer"]
    return ObserverConfig(
        latitude  = obs["latitude"],
        longitude = obs["longitude"],
    )


def _load_ingestors(raw: dict) -> dict[str, dict]:
    return raw.get("ingestors", {})


def _load_storage(raw: dict) -> StorageConfig:
    return StorageConfig(
        method = raw.get("storage", {}).get("backend", "disk_drive"),
    )


def _load_processor(raw: dict) -> ProcessorConfig | None:
    proc = raw.get("processor")
    if proc is None:
        return None
    return ProcessorConfig(
        poll_interval_seconds = proc.get("poll_interval_seconds", 5),
        modules               = proc.get("modules", []),
        display               = proc.get("display"),
    )


def _load_display(raw: dict) -> dict:
    return raw.get("display", {})


def load_config(path: Path = CONFIG_PATH) -> SquawkConfig:
    """
    Read and validate config.toml.
    Raises a clear error if the file is missing or malformed.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}\n"
            "Copy config.toml.example to config.toml and edit it."
        )

    with open(path, "rb") as f:
        raw = tomllib.load(f)

    return SquawkConfig(
        squawk    = _load_squawk(raw),
        observer  = _load_observer(raw),
        storage   = _load_storage(raw),
        ingestors = _load_ingestors(raw),
        processor = _load_processor(raw),
        display   = _load_display(raw),
        modules   = raw.get("modules", {}),
    )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
# Import this directly: from config import config

config = load_config()
