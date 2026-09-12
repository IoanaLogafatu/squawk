"""
config.py

Loads and validates config.toml.

Nothing else in Squawk reads the TOML file — everything is handed a typed
object built here, so there is one place where a bad configuration is
caught and it is caught before any process starts doing work.

Validation is strict on structure and lenient on tuning. An unknown
top-level key, an unknown chain type, an unknown transform or service, a
missing or duplicated snapshot chain, a [service.<name>] block for a
service that is not listed or with a key it does not take, an output chain
that does not say what it watches, a run profile naming a chain that does not exist: all hard
errors, reported together rather than one per run. Timings and thresholds may default, because a default poll
interval is a preference where a mistyped module name is a mistake.

Usage:
    from config import load_config
    config = load_config()
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields as dataclass_fields
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(__file__).parent / "config.toml"

# Chain families, in the order main.py accepts them on the command line.
CHAIN_FAMILIES = ("ingest_chain", "snapshot_chain", "output_chain")

TOP_LEVEL_KEYS = {
    "data_dir",
    "observer_latitude",
    "observer_longitude",
    "services",
    "service",
    "run",
    *CHAIN_FAMILIES,
}

DEBUG_LEVELS = ("error", "warn", "info", "debug")

# What an output chain watches: the live snapshot, or deletion notices.
OUTPUT_SOURCES = ("live", "deletions")

# The single snapshot chain's config key is also its name — there is only
# ever one, so it does not get a user-chosen label like the other families.
SNAPSHOT_CHAIN_NAME = "snapshot_chain"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ConfigError(Exception):
    """Raised when config.toml is invalid. The message lists every problem found."""


def _fail(errors: list[str]) -> None:
    if errors:
        raise ConfigError(
            "config.toml has %d problem(s):\n\n  - %s"
            % (len(errors), "\n  - ".join(errors))
        )


# ---------------------------------------------------------------------------
# Typed config blocks
# ---------------------------------------------------------------------------

@dataclass
class ChainConfig:
    """
    What every chain block has in common.

    name        — the config key, e.g. "concorde_A". Also the logger name.
    type        — which module implements it, e.g. "ingest_concorde".
    transform   — transform type names, applied in this order.
    debug_level — error | warn | info | debug, for this chain's modules.
    """

    name:        str
    type:        str
    transform:   list[str]  = field(default_factory=list)
    debug_level: str        = "warn"


@dataclass
class IngestChainConfig(ChainConfig):
    """
    An ingest chain: poll a source, transform, hand to the snapshot.

    The runner sleeps a random interval between polls, in the range below.
    Real sources are not metronomes and neither should the simulator be —
    it is what makes the two ingest chains interleave rather than march in
    step, which is the case the snapshot merge has to survive.
    """

    poll_min_seconds: float = 5.0
    poll_max_seconds: float = 20.0


@dataclass
class SnapshotChainConfig(ChainConfig):
    """
    The snapshot: the shared picture, plus the process that expires it.

    expiry_minutes            — an aircraft unseen for this long is deleted.
    scan_interval_seconds     — how often the expiry loop wakes.
    deletion_retention_minutes— how long a deletion notice is kept for output
                                chains to notice before it is swept up. Must
                                comfortably exceed every output chain's poll
                                interval, or a slow chain misses deletions.
    """

    expiry_minutes:             float = 5.0
    scan_interval_seconds:      float = 30.0
    deletion_retention_minutes: float = 60.0


@dataclass
class OutputChainConfig(ChainConfig):
    """
    An output chain: transform, then hand to the output module — one input,
    chosen by source.

    source — "live":      read the snapshot every poll_interval_seconds and
                          call the module's send() with the whole picture.
             "deletions": never read the snapshot; every
                          poll_interval_seconds, call on_deleted() once for
                          each aircraft that has expired since last time.

    A destination that wants both is two chain blocks of the same module
    type, one of each. Required in config.toml — defaulted here only so the
    dataclass can be built directly in tests.
    """

    source:                str   = "live"
    poll_interval_seconds: float = 5.0


@dataclass
class ServiceConfig:
    """
    What every service's settings have in common.

    name        — the service's name, e.g. "concorde". Also its logger name.
    debug_level — error | warn | info | debug.

    Read from an optional [service.<name>] block; a service listed in
    `services` with no block takes these defaults. A service with settings
    of its own subclasses this and names the subclass as its config_class
    (see services/base.py) — the keys accepted in its block come from that
    dataclass, exactly as chain blocks' do.
    """

    name:        str
    debug_level: str = "warn"


@dataclass
class RunProfile:
    """
    A named set of chains launched together by `python main.py run <name>`.

    chains — (family, name) pairs, e.g. ("ingest_chain", "concorde_A"),
             written in config.toml as "ingest_chain.concorde_A".
    """

    name:   str
    chains: list[tuple[str, str]]


@dataclass
class Config:
    """The whole installation."""

    data_dir:           Path
    observer_latitude:  float
    observer_longitude: float
    services:           list[str]
    snapshot_chain:     SnapshotChainConfig
    ingest_chains:      dict[str, IngestChainConfig]
    output_chains:      dict[str, OutputChainConfig]
    runs:               dict[str, RunProfile] = field(default_factory=dict)
    # Settings for each listed service, whether or not it had a block.
    # Filled in for any listed service left out, so a Config built directly
    # in a test still hands every service its defaults.
    service_configs:    dict[str, ServiceConfig] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in self.services:
            self.service_configs.setdefault(name, ServiceConfig(name=name))

    def profile(self, name: str) -> RunProfile:
        """The [run.<name>] profile, or ConfigError naming the ones there are."""
        if name not in self.runs:
            known = ", ".join(sorted(self.runs)) or "(none configured)"
            raise ConfigError(f"no [run.{name}] profile in config.toml — configured: {known}")
        return self.runs[name]

    def service(self, name: str) -> ServiceConfig:
        """A listed service's settings, or ConfigError naming the ones there are."""
        if name not in self.services:
            known = ", ".join(self.services) or "(none configured)"
            raise ConfigError(f"'{name}' is not in services — configured: {known}")
        return self.service_configs[name]

    def chain(self, family: str, name: str) -> ChainConfig:
        """
        The config block for one chain, as named on the command line.

        Raises ConfigError rather than KeyError so main.py can report a
        typo'd chain name the same way it reports any other config problem.
        """
        if family == "ingest_chain":
            blocks: dict[str, Any] = self.ingest_chains
        elif family == "output_chain":
            blocks = self.output_chains
        elif family == "snapshot_chain":
            blocks = {SNAPSHOT_CHAIN_NAME: self.snapshot_chain}
        else:
            raise ConfigError(
                f"unknown chain family '{family}' — expected one of "
                f"{', '.join(CHAIN_FAMILIES)}"
            )

        if name not in blocks:
            known = ", ".join(sorted(blocks)) or "(none configured)"
            raise ConfigError(
                f"no [{family}.{name}] block in config.toml — configured: {known}"
            )
        return blocks[name]


# ---------------------------------------------------------------------------
# Module type names, imported from the registries
#
# Imported lazily inside the validator so config.py stays importable on its
# own: a config-validation test should not have to import every module in
# the project to run.
# ---------------------------------------------------------------------------

def _known_types() -> dict[str, set[str]]:
    from ingest import INGESTORS
    from output import OUTPUTS
    from services import SERVICES
    from snapshot import SNAPSHOTS
    from transforms import TRANSFORMS

    return {
        "ingest_chain":   set(INGESTORS),
        "snapshot_chain": set(SNAPSHOTS),
        "output_chain":   set(OUTPUTS),
        "transform":      set(TRANSFORMS),
        "service":        set(SERVICES),
    }


def _service_config_classes() -> dict[str, type[ServiceConfig]]:
    from services import SERVICES

    return {name: cls.config_class for name, cls in SERVICES.items()}


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_config(path: Path | str | None = None) -> Config:
    """
    Read and validate config.toml.

    Args:
        path: override for the config file location, for tests.

    Raises:
        ConfigError: if the file is missing, unparseable, or invalid.
    """
    path = Path(path) if path is not None else CONFIG_PATH

    try:
        with open(path, "rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError:
        raise ConfigError(
            f"{path} not found — copy config.toml.example to config.toml and edit it"
        ) from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from None

    return build_config(raw)


def build_config(raw: dict[str, Any]) -> Config:
    """Validate an already-parsed TOML mapping. Split out so tests can call it directly."""
    errors: list[str] = []
    types = _known_types()

    unknown = sorted(set(raw) - TOP_LEVEL_KEYS)
    for key in unknown:
        errors.append(
            f"unknown top-level key '{key}' — expected one of "
            f"{', '.join(sorted(TOP_LEVEL_KEYS))}"
        )

    data_dir = Path(raw.get("data_dir", "data"))

    observer_latitude  = _require_float(raw, "observer_latitude", errors)
    observer_longitude = _require_float(raw, "observer_longitude", errors)

    services = raw.get("services", [])
    if not isinstance(services, list) or not all(isinstance(s, str) for s in services):
        errors.append("services must be a list of strings, e.g. services = [\"concorde\"]")
        services = []
    for name in services:
        if name not in types["service"]:
            errors.append(
                f"unknown service '{name}' — available: "
                f"{', '.join(sorted(types['service'])) or '(none)'}"
            )

    snapshot_chain = _load_snapshot_chain(raw, types, errors)
    ingest_chains  = _load_family(raw, "ingest_chain", IngestChainConfig, types, errors)
    output_chains  = _load_family(raw, "output_chain", OutputChainConfig, types, errors)

    # Checked against the raw tables, not the loaded ones: a chain that
    # failed validation above has already been reported, and saying "no
    # ingest chains" on top of that just buries the real error.
    if not raw.get("ingest_chain"):
        errors.append("no [ingest_chain.<name>] blocks — nothing would ever be observed")
    if not raw.get("output_chain"):
        errors.append("no [output_chain.<name>] blocks — nothing would ever be shown")

    service_configs = _load_services(raw, services, types, errors)
    runs            = _load_runs(raw, errors)

    _fail(errors)

    return Config(
        data_dir           = data_dir,
        observer_latitude  = observer_latitude,
        observer_longitude = observer_longitude,
        services           = services,
        snapshot_chain     = snapshot_chain,   # type: ignore[arg-type]  (validated above)
        ingest_chains      = ingest_chains,
        output_chains      = output_chains,
        runs               = runs,
        service_configs    = service_configs,
    )


def _load_services(
    raw:      dict[str, Any],
    services: list[str],
    types:    dict[str, set[str]],
    errors:   list[str],
) -> dict[str, ServiceConfig]:
    """
    Settings for every listed service, from its [service.<name>] block if any.

    The `services` list stays the one place that says which services run;
    a block only configures one. So a block for a service that is not
    listed is an error — it would otherwise be silently ignored.
    """
    blocks = raw.get("service", {})
    if not isinstance(blocks, dict):
        errors.append("[service] must contain named blocks, e.g. [service.concorde]")
        blocks = {}

    for name in sorted(set(blocks) - set(services)):
        errors.append(
            f"[service.{name}]: '{name}' is not in services — add it to the "
            f"services list to run it"
        )

    classes = _service_config_classes()
    loaded  = {}
    for name in services:
        if name not in types["service"]:
            continue   # already reported as an unknown service
        block = blocks.get(name, {})
        label = f"[service.{name}]"
        if not isinstance(block, dict):
            errors.append(f"{label} must be a table")
            continue

        cls     = classes[name]
        allowed = {f.name for f in dataclass_fields(cls)} - {"name"}
        problems_before = len(errors)

        for key in sorted(set(block) - allowed):
            errors.append(
                f"{label}: unknown key '{key}' — expected one of {', '.join(sorted(allowed))}"
            )
        debug_level = block.get("debug_level", "warn")
        if debug_level not in DEBUG_LEVELS:
            errors.append(
                f"{label}: debug_level '{debug_level}' — expected one of {', '.join(DEBUG_LEVELS)}"
            )

        if len(errors) != problems_before:
            continue
        try:
            loaded[name] = cls(name=name, **block)
        except TypeError as exc:
            errors.append(f"{label}: {exc}")
    return loaded


def _load_runs(raw: dict[str, Any], errors: list[str]) -> dict[str, RunProfile]:
    """
    Every [run.<name>] profile.

    Chain references are checked against the raw tables rather than the
    loaded chains, for the same reason as the "no ingest chains" check: a
    chain that failed its own validation has already been reported, and a
    profile naming it is not a second mistake.
    """
    blocks = raw.get("run", {})
    if not isinstance(blocks, dict):
        errors.append("[run] must contain named profiles, e.g. [run.my_profile]")
        return {}

    runs = {}
    for name, block in blocks.items():
        label = f"[run.{name}]"
        if not isinstance(block, dict):
            errors.append(f"{label} must be a table")
            continue

        for key in sorted(set(block) - {"chains"}):
            errors.append(f"{label}: unknown key '{key}' — expected chains")

        chains = block.get("chains")
        if not isinstance(chains, list) or not chains or \
                not all(isinstance(c, str) for c in chains):
            errors.append(
                f"{label}: chains must be a non-empty list of chain names, "
                f"e.g. chains = [\"ingest_chain.concorde_A\"]"
            )
            continue

        problems_before = len(errors)
        parsed = []
        for entry in chains:
            family, _, chain_name = entry.partition(".")
            if family not in CHAIN_FAMILIES or not chain_name:
                errors.append(
                    f"{label}: '{entry}' is not <family>.<name> — family must be one of "
                    f"{', '.join(CHAIN_FAMILIES)}"
                )
                continue
            if not _chain_is_defined(raw, family, chain_name):
                errors.append(f"{label}: '{entry}' — no such chain in config.toml")
                continue
            if (family, chain_name) in parsed:
                errors.append(f"{label}: '{entry}' is listed twice")
                continue
            parsed.append((family, chain_name))

        if len(errors) == problems_before:
            runs[name] = RunProfile(name=name, chains=parsed)
    return runs


def _chain_is_defined(raw: dict[str, Any], family: str, name: str) -> bool:
    if family == "snapshot_chain":
        return name == SNAPSHOT_CHAIN_NAME and isinstance(raw.get(SNAPSHOT_CHAIN_NAME), dict)
    blocks = raw.get(family)
    return isinstance(blocks, dict) and isinstance(blocks.get(name), dict)


def _load_snapshot_chain(
    raw: dict[str, Any], types: dict[str, set[str]], errors: list[str]
) -> SnapshotChainConfig | None:
    """
    The one and only snapshot chain.

    Unlike the other families this is a single block, not a table of named
    blocks. Writing it as [snapshot_chain.something] — a second snapshot,
    in other words — is the error the brief asks us to catch; TOML itself
    rejects the other way of writing two, a repeated [snapshot_chain]
    header, before we ever see it.
    """
    block = raw.get(SNAPSHOT_CHAIN_NAME)

    if block is None:
        errors.append("no [snapshot_chain] block — exactly one is required")
        return None
    if not isinstance(block, dict):
        errors.append("[snapshot_chain] must be a table")
        return None

    nested = sorted(k for k, v in block.items() if isinstance(v, dict))
    if nested:
        errors.append(
            "exactly one snapshot chain is allowed, but [snapshot_chain] contains "
            f"named sub-blocks: {', '.join(nested)}"
        )
        return None

    return _build_block(
        SNAPSHOT_CHAIN_NAME, block, SnapshotChainConfig, "snapshot_chain", types, errors
    )


def _load_family(
    raw: dict[str, Any],
    family: str,
    cls: type,
    types: dict[str, set[str]],
    errors: list[str],
) -> dict[str, Any]:
    """Load every [<family>.<name>] block."""
    blocks = raw.get(family, {})
    if not isinstance(blocks, dict):
        errors.append(f"[{family}] must contain named blocks, e.g. [{family}.my_chain]")
        return {}

    loaded = {}
    for name, block in blocks.items():
        if not isinstance(block, dict):
            errors.append(f"[{family}.{name}] must be a table")
            continue
        built = _build_block(name, block, cls, family, types, errors)
        if built is not None:
            loaded[name] = built
    return loaded


def _build_block(
    name: str,
    block: dict[str, Any],
    cls: type,
    family: str,
    types: dict[str, set[str]],
    errors: list[str],
) -> Any:
    """
    Turn one config table into its typed object.

    Accepted keys come from the dataclass itself, so a field added to a
    chain config above is accepted here with no second edit — and anything
    not on the dataclass is rejected rather than silently ignored.
    """
    label   = f"[{family}.{name}]" if family != "snapshot_chain" else "[snapshot_chain]"
    allowed = {f.name for f in dataclass_fields(cls)} - {"name"}

    problems_before = len(errors)

    for key in sorted(set(block) - allowed):
        errors.append(
            f"{label}: unknown key '{key}' — expected one of {', '.join(sorted(allowed))}"
        )

    chain_type = block.get("type")
    if chain_type is None:
        errors.append(f"{label}: missing 'type'")
    elif chain_type not in types[family]:
        errors.append(
            f"{label}: unknown type '{chain_type}' — available: "
            f"{', '.join(sorted(types[family])) or '(none)'}"
        )

    transform = block.get("transform", [])
    if not isinstance(transform, list) or not all(isinstance(t, str) for t in transform):
        errors.append(f"{label}: transform must be a list of transform names")
        transform = []
    else:
        for t in transform:
            if t not in types["transform"]:
                errors.append(
                    f"{label}: unknown transform '{t}' — available: "
                    f"{', '.join(sorted(types['transform']))}"
                )

    debug_level = block.get("debug_level", "warn")
    if debug_level not in DEBUG_LEVELS:
        errors.append(
            f"{label}: debug_level '{debug_level}' — expected one of {', '.join(DEBUG_LEVELS)}"
        )
        debug_level = "warn"

    if family == "output_chain":
        source = block.get("source")
        if source is None:
            errors.append(
                f"{label}: missing 'source' — expected one of {', '.join(OUTPUT_SOURCES)}"
            )
        elif source not in OUTPUT_SOURCES:
            errors.append(
                f"{label}: source '{source}' — expected one of {', '.join(OUTPUT_SOURCES)}"
            )

    if len(errors) != problems_before:
        return None

    kwargs = {k: v for k, v in block.items() if k in allowed}
    kwargs.update(name=name, transform=transform, debug_level=debug_level)
    try:
        return cls(**kwargs)
    except TypeError as exc:
        errors.append(f"{label}: {exc}")
        return None


def _require_float(raw: dict[str, Any], key: str, errors: list[str]) -> float:
    value = raw.get(key)
    if value is None:
        errors.append(f"missing '{key}' — Squawk needs to know where it is observing from")
        return 0.0
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        errors.append(f"'{key}' must be a number, got {value!r}")
        return 0.0
    return float(value)
