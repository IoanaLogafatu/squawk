"""
config.py

Loads and validates config.toml.

All other modules import from here — nothing reads the TOML file directly.
This gives one place to catch configuration errors before anything starts.

Structure must be declared explicitly (every block a chain or ingestor
references, and the handful of keys that change what the pipeline does);
tuning knobs with a sensible default may still default. See ConfigError
for the full set of rules enforced at load time.

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
# Errors
# ---------------------------------------------------------------------------

class ConfigError(Exception):
    """Raised when config.toml is invalid. Message lists every problem found."""


def _fail(errors: list[str]) -> None:
    if errors:
        raise ConfigError(
            "config.toml has %d problem(s):\n\n  - %s"
            % (len(errors), "\n  - ".join(errors))
        )


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
    backend: str   # e.g. "disk_drive"


@dataclass
class ProcessorConfig:
    name:                  str                  # Identifier for the processor chain
    modules:               list[str]            # Module names, applied in order
    display:               str                  # Display module name
    enabled:               bool                 # Whether this processor chain is active
    poll_interval_seconds: int        = 5        # How often the processor runs


@dataclass
class SquawkConfig:
    squawk:     SquawkSystemConfig
    observer:   ObserverConfig
    storage:    StorageConfig
    ingestors:  dict[str, dict]
    processors: dict[str, ProcessorConfig] = field(default_factory=dict)
    display:    dict = field(default_factory=dict)   # Per-display config keyed by module name
    modules:    dict = field(default_factory=dict)   # Per-module config keyed by module name


# ---------------------------------------------------------------------------
# Section loaders — each appends to `errors` and returns a usable (possibly
# placeholder) value so later checks can still run. Nothing here raises;
# load_config() raises once, at the end, with every problem found.
# ---------------------------------------------------------------------------

def _load_squawk(raw: dict, errors: list[str]) -> SquawkSystemConfig:
    squawk = raw.get("squawk")
    if not isinstance(squawk, dict) or "data_dir" not in squawk:
        errors.append("[squawk] is missing a 'data_dir' key — set the working data directory.")
        return SquawkSystemConfig(data_dir=Path("data"))
    return SquawkSystemConfig(data_dir=Path(squawk["data_dir"]))


def _load_observer(raw: dict, errors: list[str]) -> ObserverConfig:
    obs = raw.get("observer")
    if not isinstance(obs, dict):
        errors.append("[observer] is missing — set 'latitude' and 'longitude'.")
        return ObserverConfig(latitude=0.0, longitude=0.0)
    missing = [k for k in ("latitude", "longitude") if k not in obs]
    if missing:
        errors.append(f"[observer] is missing {', '.join(missing)} — set decimal degrees for each.")
    return ObserverConfig(
        latitude  = obs.get("latitude", 0.0),
        longitude = obs.get("longitude", 0.0),
    )


def _load_storage(raw: dict, errors: list[str]) -> StorageConfig:
    storage = raw.get("storage")
    if not isinstance(storage, dict) or "backend" not in storage:
        errors.append('[storage] is missing a \'backend\' key — e.g. backend = "disk_drive".')
        return StorageConfig(backend="disk_drive")
    return StorageConfig(backend=storage["backend"])


def _load_ingestors(raw: dict, errors: list[str]) -> dict[str, dict]:
    ingestors = raw.get("ingestors", {})
    if not isinstance(ingestors, dict):
        return {}

    for name, cfg in ingestors.items():
        if not isinstance(cfg, dict):
            continue
        if "enabled" not in cfg:
            errors.append(f"[ingestors.{name}] has no 'enabled' key — set enabled = true or false.")
        if name == "personal_adsb" and "receivers" not in cfg:
            errors.append(
                f"[ingestors.{name}] has no 'receivers' key — list at least one receiver, "
                "or set enabled = false."
            )

    return ingestors


def _load_processors(raw: dict, errors: list[str]) -> dict[str, ProcessorConfig]:
    processors: dict[str, ProcessorConfig] = {}

    raw_processors = raw.get("processors", {})
    if not isinstance(raw_processors, dict):
        return processors

    for name, p in raw_processors.items():
        if not isinstance(p, dict):
            continue

        required = ("enabled", "modules", "display")
        missing = [k for k in required if k not in p]
        if missing:
            errors.append(
                f"[processors.{name}] is missing {', '.join(missing)} — "
                "enabled, modules and display must all be set explicitly "
                "(modules = [] is fine if the chain has no filters)."
            )
            continue

        processors[name] = ProcessorConfig(
            name                  = name,
            modules               = p["modules"],
            display               = p["display"],
            enabled               = p["enabled"],
            poll_interval_seconds = p.get("poll_interval_seconds", 5),
        )

    return processors


def _load_display(raw: dict) -> dict:
    return raw.get("display", {})


# ---------------------------------------------------------------------------
# Cross-section checks — these depend on more than one section, so they run
# after all sections have been loaded.
# ---------------------------------------------------------------------------

def _check_module_blocks(
    processors: dict[str, ProcessorConfig],
    ingestors:  dict[str, dict],
    modules:    dict,
    errors:     list[str],
) -> None:
    for name, p in processors.items():
        for mod_name in p.modules:
            if mod_name not in modules:
                errors.append(
                    f"[processors.{name}] references module '{mod_name}' but there is no "
                    f"[modules.{mod_name}] block. Add one — it may be empty."
                )

    for ing_name, cfg in ingestors.items():
        if not isinstance(cfg, dict):
            continue
        for mod_name in cfg.get("modules", []):
            if mod_name not in modules:
                errors.append(
                    f"[ingestors.{ing_name}] references module '{mod_name}' but there is no "
                    f"[modules.{mod_name}] block. Add one — it may be empty."
                )


def _check_display_blocks(
    processors: dict[str, ProcessorConfig],
    display:    dict,
    errors:     list[str],
) -> None:
    for name, p in processors.items():
        if p.display not in display:
            errors.append(
                f"[processors.{name}] uses display '{p.display}' but there is no "
                f"[display.{p.display}] block."
            )


PANEL_LAYOUTS = ("card", "list")


def _check_panel_layout(name: str, panel: dict, errors: list[str]) -> None:
    """Validate one panel's layout/bands pair.

    Deliberately does NOT check the band letters against
    [modules.altitude_band].edges. The loader can see both, but checking would
    couple display config to module config. A 'bands' entry of "F" in a
    four-band installation renders a permanently empty row — visible on the
    wall, and self-diagnosing.
    """
    layout = panel.get("layout", "card")
    if layout not in PANEL_LAYOUTS:
        errors.append(
            f"[display.http.panels.{name}] has layout = {layout!r} — must be "
            f"{' or '.join(repr(v) for v in PANEL_LAYOUTS)}."
        )
        return

    bands = panel.get("bands")

    if layout != "list":
        if bands is not None:
            errors.append(
                f"[display.http.panels.{name}] has a 'bands' key but layout = {layout!r} — "
                "'bands' only applies to layout = \"list\"."
            )
        return

    if not isinstance(bands, list) or not bands:
        errors.append(
            f"[display.http.panels.{name}] has layout = \"list\" but no 'bands' — set an "
            "ordered, non-empty list of band letters, e.g. bands = [\"D\", \"C\", \"B\", \"A\"]."
        )
        return

    seen: set[str] = set()
    for band in bands:
        if not isinstance(band, str) or len(band) != 1 or not ("A" <= band <= "Z"):
            errors.append(
                f"[display.http.panels.{name}] has bands entry {band!r} — each entry must "
                "be a single band letter from A to Z."
            )
            continue
        if band in seen:
            errors.append(
                f"[display.http.panels.{name}] lists band {band!r} twice — each row needs "
                "a band of its own, the same way each panel needs a slot of its own."
            )
        seen.add(band)


def _check_http_panels(
    processors: dict[str, ProcessorConfig],
    display:    dict,
    errors:     list[str],
) -> None:
    http_cfg = display.get("http")
    if not isinstance(http_cfg, dict):
        return   # already reported by _check_display_blocks if 'http' is actually used

    panels = http_cfg.get("panels", {})
    if not isinstance(panels, dict):
        panels = {}

    claimed: dict[int, str] = {}   # slot -> the chain that got there first

    for name, p in processors.items():
        if p.display != "http":
            continue

        if name not in panels:
            errors.append(
                f"[processors.{name}] uses the http display but there is no "
                f"[display.http.panels.{name}] block."
            )
            continue

        panel = panels[name]
        if not isinstance(panel, dict):
            errors.append(f"[display.http.panels.{name}] is not a table.")
            continue

        if "order" in panel:
            errors.append(
                f"[display.http.panels.{name}] has an 'order' key — rename it to "
                "'slot'. Panels no longer sort; each one names a fixed position "
                "in the 4x2 wall (1-4 across the top, 5-8 across the bottom)."
            )

        _check_panel_layout(name, panel, errors)

        if "slot" not in panel:
            errors.append(
                f"[display.http.panels.{name}] is missing a 'slot' key — set an "
                "integer from 1 to 8 giving its position in the 4x2 wall."
            )
            continue

        slot = panel["slot"]
        if isinstance(slot, bool) or not isinstance(slot, int) or not 1 <= slot <= 8:
            errors.append(
                f"[display.http.panels.{name}] has slot = {slot!r} — must be an "
                "integer from 1 to 8."
            )
            continue

        if slot in claimed:
            errors.append(
                f"[display.http.panels.{name}] claims slot {slot}, but "
                f"[display.http.panels.{claimed[slot]}] already claims it — "
                "each panel needs a slot of its own."
            )
            continue

        claimed[slot] = name


def _warn_unreferenced_blocks(
    processors: dict[str, ProcessorConfig],
    ingestors:  dict[str, dict],
    modules:    dict,
    display:    dict,
) -> None:
    referenced_modules: set[str] = set()
    for p in processors.values():
        referenced_modules.update(p.modules)
    for cfg in ingestors.values():
        if isinstance(cfg, dict):
            referenced_modules.update(cfg.get("modules", []))

    for name in modules:
        if name not in referenced_modules:
            print(f"  config: [modules.{name}] is not referenced by any chain — ignored")

    referenced_displays = {p.display for p in processors.values()}
    for name in display:
        if name not in referenced_displays:
            print(f"  config: [display.{name}] is not referenced by any chain — ignored")


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

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

    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            f"{path} is not valid TOML: {exc}\n"
            "  Check the syntax at that location — e.g. an unquoted string, "
            "a missing quote, or a stray key."
        ) from None

    errors: list[str] = []

    squawk     = _load_squawk(raw, errors)
    observer   = _load_observer(raw, errors)
    storage    = _load_storage(raw, errors)
    ingestors  = _load_ingestors(raw, errors)
    processors = _load_processors(raw, errors)
    display    = _load_display(raw)
    modules    = raw.get("modules", {})

    _check_module_blocks(processors, ingestors, modules, errors)
    _check_display_blocks(processors, display, errors)
    _check_http_panels(processors, display, errors)

    _fail(errors)

    _warn_unreferenced_blocks(processors, ingestors, modules, display)

    return SquawkConfig(
        squawk     = squawk,
        observer   = observer,
        storage    = storage,
        ingestors  = ingestors,
        processors = processors,
        display    = display,
        modules    = modules,
    )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
# Import this directly: from config import config

config = load_config()
