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
    name:                  str                  # Identifier for the processor chain
    poll_interval_seconds: int        = 5       # How often the processor runs
    modules:               list[str]  = field(default_factory=list) # Module names, applied in order
    display:               str | None = None    # Display module name
    enabled:               bool       = True    # Whether this processor chain is active



@dataclass
class SquawkConfig:
    squawk:     SquawkSystemConfig
    observer:   ObserverConfig
    storage:    StorageConfig
    ingestors:  dict[str, dict]
    processors: dict[str, ProcessorConfig] = field(default_factory=dict)
    display:    dict = field(default_factory=dict)   # Per-display config keyed by module name
    modules:    dict = field(default_factory=dict)   # Per-module config keyed by module name

    @property
    def processor(self) -> ProcessorConfig | None:
        """Backwards compatibility for single processor access."""
        if not self.processors:
            return None
        for p in self.processors.values():
            if p.enabled:
                return p
        return next(iter(self.processors.values()), None)


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


def _load_processors(raw: dict) -> dict[str, ProcessorConfig]:
    processors: dict[str, ProcessorConfig] = {}

    # 1. Multi-processor syntax: [processors.<name>]
    raw_processors = raw.get("processors", {})
    if isinstance(raw_processors, dict):
        for name, p in raw_processors.items():
            if not isinstance(p, dict):
                continue
            processors[name] = ProcessorConfig(
                name                  = name,
                enabled               = p.get("enabled", True),
                poll_interval_seconds = p.get("poll_interval_seconds", 5),
                modules               = p.get("modules", []),
                display               = p.get("display"),
            )

    # 2. Legacy single processor syntax: [processor]
    legacy_proc = raw.get("processor")
    if legacy_proc and isinstance(legacy_proc, dict):
        if "default" not in processors:
            processors["default"] = ProcessorConfig(
                name                  = "default",
                enabled               = legacy_proc.get("enabled", True),
                poll_interval_seconds = legacy_proc.get("poll_interval_seconds", 5),
                modules               = legacy_proc.get("modules", []),
                display               = legacy_proc.get("display"),
            )

    return processors


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
        squawk     = _load_squawk(raw),
        observer   = _load_observer(raw),
        storage    = _load_storage(raw),
        ingestors  = _load_ingestors(raw),
        processors = _load_processors(raw),
        display    = _load_display(raw),
        modules    = raw.get("modules", {}),
    )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
# Import this directly: from config import config

config = load_config()
