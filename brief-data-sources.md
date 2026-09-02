# Brief: `data_sources` — shared external dataset infrastructure

## Goal

A generic, top-level config section for external datasets that more than one
module may need to share: one download, one freshness check, one instance —
regardless of how many modules reference it.

This exists because `tar1090_db` already has this exact shape (download,
check age, rebuild) built privately into one module. The next piece of work —
loading VRS standing-data, which offers routes, aircraft, airlines, airports
and more from a single dataset — needs the same shared-download behaviour
across multiple *future* modules (`vrs_route`, and later `vrs_aircraft`)
without either module owning the download itself, or two modules racing to
fetch the same file independently.

**Out of scope for this brief:** no VRS-specific code. No `vrs_route` module.
No CSV parsing, no table design, no callsign lookups. This is the shared
plumbing only, proven against a trivial fake source type. `tar1090_db` is not
migrated onto it — it keeps its current private implementation untouched.
Whether `tar1090_db` moves onto this infrastructure later is a separate
decision for a separate brief.

---

## Config shape

A new top-level section, structurally parallel to `[ingestors.*]` and
`[modules.*]`:

```toml
[data_sources.vrs]
type = "vrs_standing_data"    # resolves to data_sources/vrs_standing_data.py
# remaining keys are free-form, validated by that type's own KEYS
```

A module opts into sharing a source by naming it:

```toml
[modules.vrs_route]
source = "vrs"
```

`source` is a new, optional, universally-recognised key on any module's config
block — not type-specific — so add it to the *common* set every module's
`KEYS` check tolerates, the same way `type` already is (see
`get_module`'s existing `unknown = set(cfg) - keys` check — `source` needs the
same treatment as `type`, added once at the factory level rather than in every
module's own `KEYS`).

---

## `config.py` changes

New dataclass, same shape as the existing section dataclasses:

```python
@dataclass
class DataSourceConfig:
    name: str
    type: str
    cfg:  dict   # the full raw block, including 'type' — sources validate their own keys
```

New loader, same pattern as `_load_ingestors`:

```python
def _load_data_sources(raw: dict, errors: list[str]) -> dict[str, DataSourceConfig]:
    sources = raw.get("data_sources", {})
    if not isinstance(sources, dict):
        return {}
    result = {}
    for name, cfg in sources.items():
        if not isinstance(cfg, dict) or "type" not in cfg:
            errors.append(f"[data_sources.{name}] is missing a 'type' key.")
            continue
        result[name] = DataSourceConfig(name=name, type=cfg["type"], cfg=cfg)
    return result
```

New cross-section check, same pattern as `_check_module_blocks`: any module
block naming `source = "x"` must have a matching `[data_sources.x]` block.
Check both `[modules.*]` and any ingestor-level module references, same two
places `_check_module_blocks` already covers.

`SquawkConfig` gains `data_sources: dict[str, DataSourceConfig] = field(default_factory=dict)`.
`load_config()` calls the new loader and the new cross-check alongside the
existing ones.

An unreferenced `[data_sources.*]` block should warn the same way
`_warn_unreferenced_blocks` already warns for unused module blocks — extend
that function rather than writing a parallel one.

---

## `data_sources/` package

New top-level package, deliberately parallel to `modules/`:

```
data_sources/
    __init__.py     # BaseDataSource, get_data_source() factory, pooling
```

```python
class BaseDataSource(ABC):
    @abstractmethod
    def ensure_fresh(self) -> None:
        """Check staleness and refresh if needed. Called by any module that
        uses this source, as often as that module likes — cheap to call
        repeatedly; the source itself decides how often a check actually
        does anything."""

    @property
    @abstractmethod
    def directory(self) -> Path:
        """Where this source's files live on disk."""
```

Concrete source types decide their own refresh *policy* — this is
deliberately not prescribed here, because tonight's session already found two
different shapes: `tar1090_db`'s "stale after N days, checked periodically"
and VRS's "publishes once daily at a known hour, have I got today's yet."
Forcing one policy shape onto both would be wrong. `BaseDataSource` only
guarantees `ensure_fresh()` is idempotent and cheap to call often.

`get_data_source(name, cfg) -> BaseDataSource`: same factory shape as
`get_module` — resolves `cfg["type"]` to `data_sources.<type>`, pools
instances by `(name, cfg)` so two modules naming the same `source` share one
instance and therefore one download, imports the type module and calls its
`get(cfg, ctx)`. A `DataSourceContext` (or reuse `ModuleContext` if the
fields genuinely overlap — check before inventing a second dataclass) gives
the type module its own subdirectory: `<data_dir>/data_sources/<name>/`, kept
separate from `<data_dir>/modules/<module_name>/` since a source belongs to
no single module.

---

## Wiring a module to its source

`ModuleContext` gains a way to resolve a named source:

```python
@dataclass(frozen=True)
class ModuleContext:
    data_dir:    Path
    module_dir:  Path
    observer:    "ObserverConfig"
    data_source: Callable[[str], "BaseDataSource"]   # resolves a source by name
```

A module that wants to share a source calls `ctx.data_source(cfg["source"])`
in its own `get()`, then calls `.ensure_fresh()` and reads `.directory` as
needed — same pattern as a module currently calling `ctx.module_dir` directly,
just resolved through a name instead of being handed automatically. A module
with no `source` key simply doesn't call it.

---

## Tests

New `tests/test_data_sources.py`. Build a minimal fake source type for these
tests — check the existing convention for how `test_modules.py` fabricates an
unknown/fake module type for its error-path tests, and mirror it rather than
inventing a new pattern.

1. `[data_sources.x]` with no `type` fails, naming the block.
2. A module referencing `source = "x"` with no matching `[data_sources.x]`
   fails, naming both.
3. A module referencing a source that exists passes validation.
4. Two modules naming the same `source` in config resolve to the same
   `BaseDataSource` instance (pooling proof — same object identity).
5. Two different `[data_sources.*]` blocks of the same `type` but different
   config produce two distinct instances (config is part of the pool key,
   same as `get_module`).
6. `ensure_fresh()` is idempotent — calling it repeatedly on a fake source
   whose "fetch" is a counter only actually fetches once, then again after a
   configurable staleness window elapses (prove this at the fake-source level;
   the real policy logic belongs to each concrete type later).
7. An unreferenced `[data_sources.*]` block triggers the existing
   unreferenced-block warning.
8. `directory` for a source is distinct from any module's `module_dir` and
   sits under `<data_dir>/data_sources/<name>/`.

Extend `tests/test_config.py` with the loader- and cross-check-level cases
from 1–3 above, matching the existing style for `_load_ingestors` and
`_check_module_blocks` tests.

---

## Docs

New section in `docs/modules-guide.md` (or a new `docs/data-sources-guide.md`
if the modules guide is already long) explaining: what a data source is, when
a module should use one versus managing its own download privately (a source
used by exactly one module today may still be worth it if a second module is
expected soon — `tar1090_db` not being migrated is a judgement call about
priority, not a statement that private-download modules are wrong), and the
`ensure_fresh()` contract concrete types must honour — cheap and safe to call
often, refresh policy is the type's own business.

## Next

`vrs_route`: a `data_sources/vrs_standing_data.py` type that downloads the CSV
set, and a `modules/vrs_route.py` that uses it via `source = "vrs"` to answer
callsign → route lookups. Built once this brief has landed and its tests pass
on their own, so the VRS-specific work sits on infrastructure that's already
been proven rather than being designed and tested together.
