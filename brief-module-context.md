# Brief: pass a context to modules instead of reaching for global config

**Scope:** `modules/__init__.py`, `display/__init__.py`, all eight files in `modules/`,
all four packages in `display/`, `docs/modules-guide.md`, `docs/display-guide.md`,
`docs/modules-reference.md`, and the affected tests.

**Do not** change `config.py`, `config.toml`, `storage/`, `ingestor/`, `schemas/`,
`processor/processor.py`, or any module's filtering logic.

No config change. No behaviour change — this alters how modules *obtain* two values they
already use, not what they do with them.

> **Breaking contract change.** Every module and display `get()` gains a second
> parameter. All twelve shipping ones are updated here. This is the right moment: V1 is
> unpublished, so no third-party module exists to break.

---

## Problem

Four modules and two displays reach into global config from inside their factory:

```python
# modules/adsbdb.py, modules/tar1090_db.py
from config import config as squawk_config
data_dir = Path(squawk_config.squawk.data_dir)
```

```python
# display/epaper/__init__.py, display/pushover/__init__.py
data_dir = Path(cfg.get("data_dir", squawk_config.squawk.data_dir))
```

```python
# modules/ground_distance_filter.py
try:
    from config import config as squawk_config
    if squawk_config and squawk_config.observer:
        ...
except Exception:
    pass
```

Three consequences.

**It is the pattern a contributor will copy**, because `get(cfg)` offers no other way to
find the data directory. Nothing in `docs/modules-guide.md` says where a module should
write, so the first contributed enricher that needs a cache will import global config too.

**It makes modules untestable without faking an import.** `test_module_adsbdb.py` has to
do this:

```python
monkeypatch.setitem(__import__("sys").modules, "config",
                    type("M", (), {"config": _Cfg()}))
```

`test_tar1090_db.py` mutates `squawk_config.squawk.data_dir` on the live config object.
`display/pushover` and `display/epaper` grew an undocumented `data_dir` config key that
exists only so their tests can override the path — a magic key that appears in no
example, no docs, and no `KEYS` set.

**One of them swallows errors.** `ground_distance_filter`'s bare `except Exception: pass`
means a missing observer produces a filter where `_get_ground_distance_nm` returns `None`
for every aircraft without `r_dst`, so the panel silently empties. `config.py` now
requires `[observer]`, so the guard protects against nothing and hides real failures.

---

## Design

The factory reaches for global config once. Modules receive what they need.

In `modules/__init__.py`, alongside `BaseModule`:

```python
from dataclasses import dataclass
from pathlib import Path

if TYPE_CHECKING:
    from config import ObserverConfig


@dataclass(frozen=True)
class ModuleContext:
    """Everything a module may need from the wider installation.

    Built by the factory, one per module. Modules that need neither field
    accept it and ignore it — the signature is uniform.
    """
    data_dir:   Path              # installation data directory
    module_dir: Path              # this module's own directory; not created
    observer:   "ObserverConfig"  # receiver position
```

Every module and display factory becomes:

```python
def get(cfg: dict, ctx: ModuleContext) -> BaseModule:
```

**`module_dir` is keyed on the resolved module type, not the config block name.**
`data_dir / "modules" / module_type` for modules, `data_dir / "display" / name` for
displays. This preserves every existing path exactly — `data/modules/adsbdb`,
`data/modules/tar1090_db`, `data/display/epaper`, `data/display/pushover` — so no cache
is orphaned and no SQLite index is rebuilt on deploy.

It is also the correct semantics. A cache is a property of the data source, not of the
config block that happens to reference it. Two aliased `adsbdb` blocks should share one
disk cache and one `tar1090_db` index. This differs deliberately from the *instance* pool,
which keys on block name — same object per block, same directory per type.

**`module_dir` is not created by the factory.** A module that never writes should not
leave an empty directory behind. Modules that write keep their existing `mkdir(parents=True,
exist_ok=True)` call.

**The context does not affect the instance pool key.** It is derived from global config,
which is constant for the process lifetime.

---

## Change 1 — build the context in the factories

`modules/__init__.py`:

```python
def get_module(name: str, cfg: dict | None = None) -> BaseModule:
    cfg = cfg or {}
    key = (name, _cfg_key(cfg))
    with _INSTANCES_LOCK:
        if key not in _INSTANCES:
            module_type = cfg.get("type", cfg.get("module", name))
            try:
                module = importlib.import_module(f"modules.{module_type}")
            except ModuleNotFoundError:
                raise ValueError(f"Unknown module: {name!r} (resolved to {module_type!r})")

            # ... existing KEYS check ...

            from config import config as squawk_config
            data_dir = Path(squawk_config.squawk.data_dir)
            ctx = ModuleContext(
                data_dir   = data_dir,
                module_dir = data_dir / "modules" / module_type,
                observer   = squawk_config.observer,
            )
            _INSTANCES[key] = module.get(cfg, ctx)
        return _INSTANCES[key]
```

Keep `from config import config` inside the function body, not at module import time —
importing `modules` should not trigger a config load.

`display/__init__.py` does the same with `data_dir / "display" / name`. Displays are named
directly (`display = "http"`), never aliased, so name and type coincide.

`processor/processor.py` is unchanged — it calls `get_display(name, cfg)` and the factory
builds the context itself.

---

## Change 2 — update the modules

**`adsbdb`** — `get()` drops the config import and uses `ctx.module_dir` as `cache_dir`,
keeping its existing `mkdir`.

**`tar1090_db`** — `get()` drops the config import; `csv_path` and `db_path` derive from
`ctx.module_dir`. Note this flattens the path by one level: currently
`data_dir / "modules" / "tar1090_db" / "aircraft.csv"`, which is exactly what
`ctx.module_dir / "aircraft.csv"` gives. **No file moves.**

**`ground_distance_filter`** — delete the entire `try/except` block. Observer position
comes from `ctx.observer`, with the explicit `observer_lat` / `observer_lon` config keys
still taking precedence when set:

```python
obs_lat = cfg.get("observer_lat", ctx.observer.latitude)
obs_lon = cfg.get("observer_lon", ctx.observer.longitude)
```

**`altitude_filter`, `vertical_rate_filter`, `registration_filter`, `closest_filter`,
`pass_through`** — signature only. They accept `ctx` and ignore it.

---

## Change 3 — update the displays

**`pushover`** — `PushoverDisplay.__init__` takes `(cfg, ctx)`. The state file paths come
from `ctx.module_dir` rather than `data_dir / "display" / "pushover"`. **Remove the
`cfg.get("data_dir", ...)` override entirely** — it exists only for tests, which get a
context now.

**`epaper`** — same treatment; `png_path` becomes `ctx.module_dir / "squawk_display.png"`.
Remove its `data_dir` config key too.

**`http`, `console`** — signature only.

---

## Change 4 — documentation

`docs/modules-guide.md`, in the "Writing your own" checklist, replace the factory item:

> 2. **Add the factory:** `def get(cfg: dict, ctx: ModuleContext) -> YourModule`.

and add:

> **Use `ctx`, not global config.** `ctx.module_dir` is your module's directory for
> caches and state — create it when you first write, and never write anywhere else.
> `ctx.observer` is the receiver position. `ctx.data_dir` is the installation root, which
> you should rarely need. Importing `config` directly inside a module works today but
> couples you to the whole configuration and makes your module impossible to test without
> faking the import.
>
> Modules that need none of this still take `ctx` and ignore it — the signature is
> uniform so the factory never has to ask what a module wants.
>
> The directory is keyed on module *type*, so two config blocks aliasing the same module
> share one cache. That is usually what you want. If it is not, key your files within the
> directory.

`docs/display-guide.md` gets the same for displays, plus a note that the `data_dir`
config key is gone.

`docs/modules-reference.md` — check the `adsbdb` and `tar1090_db` entries still describe
their cache locations correctly. The paths do not change, so this is verification, not
rewriting.

---

## Change 5 — tests

The point of this change is that these get simpler.

**`test_module_adsbdb.py`** — delete the `sys.modules["config"]` fake in
`reset_shared_instance`. It becomes a `clear_module_pool()` call plus a locally built
context:

```python
@pytest.fixture
def ctx(tmp_path):
    from modules import ModuleContext
    from config import ObserverConfig
    return ModuleContext(
        data_dir   = tmp_path,
        module_dir = tmp_path / "modules" / "adsbdb",
        observer   = ObserverConfig(latitude=53.7778, longitude=-1.5721),
    )
```

Tests calling `adsbdb_module.get({})` become `adsbdb_module.get({}, ctx)`.

**`test_tar1090_db.py`** — `test_missing_csv_returns_noop_enricher` drops
`monkeypatch.setattr(squawk_config.squawk, "data_dir", ...)` and passes a context with
`module_dir` under `tmp_path`. No more mutating the live config object.

**`test_pushover.py`** — the twelve-odd `PushoverDisplay({"data_dir": str(tmp_path)})`
constructions become `PushoverDisplay(cfg, ctx)` with a `tmp_path`-rooted context. The
`data_dir` key is gone from cfg entirely.

**`test_ground_distance_filter.py`** — add a case proving observer position arrives from
the context: a filter built with no `observer_lat`/`observer_lon` in cfg correctly
computes haversine distance for an aircraft with lat/lon but no `r_dst`. This is the
behaviour the swallowed exception could previously break silently.

New tests in `test_modules.py`:

1. **Every module accepts the two-argument signature** — iterate `modules/` with
   `pkgutil`, call each `get({}, ctx)`, assert a `BaseModule` comes back. This is the
   test that catches the next module added with the old signature, in the same shape as
   the `KEYS` test.
2. **`module_dir` is keyed on type, not block name** —
   `get_module("adsbdb_tv", {"type": "adsbdb"})` produces a module whose cache directory
   ends in `adsbdb`, not `adsbdb_tv`.
3. **The factory does not create `module_dir`** — after `get_module("pass_through")`,
   `data_dir / "modules" / "pass_through"` does not exist.

Same for displays in `test_http_display.py` or wherever the display factory is covered.

---

## Explicitly out of scope

- Any change to `config.py`, including the shape of `ObserverConfig`.
- Passing storage or the module pool into the context. `ctx` carries configuration
  values, not services. Adding a storage handle would let a module write outside its own
  directory, which is what this brief is closing off.
- `KEYS` for displays.
- The `adsbdb` cache stampede.
- Ingest modules being opt-in per ingestor.
