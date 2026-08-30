# Brief: pool module instances in the factory

**Scope:** `modules/__init__.py`, `modules/adsbdb.py`, `modules/tar1090_db.py`,
`docs/modules-guide.md`, `docs/modules-reference.md`, `tests/test_modules.py`,
`tests/test_module_adsbdb.py`, `tests/test_tar1090_db.py`.

**Do not** touch `processor/`, `storage/`, `display/`, `ingestor/`, `config.py`,
`config.toml`, or any filter module.

No config change. No behaviour change for any currently shipping configuration — this
moves an existing guarantee from two modules into the factory that should have provided
it.

---

## Problem

`get_storage()` pools backends by `(method, data_dir)`. `get_module()` pools nothing:

```python
def get_module(name: str, cfg: dict | None = None) -> BaseModule:
    cfg = cfg or {}
    module_type = cfg.get("type", cfg.get("module", name))
    module = importlib.import_module(f"modules.{module_type}")
    return module.get(cfg)          # fresh call, every time
```

Eight chains referencing `adsbdb` means eight `get()` calls. The shared instance that
`brief-adsbdb-shared-instance.md` delivered comes from `modules/adsbdb.py` hand-rolling a
module-level `_INSTANCE` plus `_INSTANCE_LOCK` and ignoring `cfg` entirely.
`modules/tar1090_db.py` does the same with `_SQLITE_INSTANCE` and `_SQLITE_LOCK`.

Two problems follow.

**The published contract is wrong.** A contributed enrichment module — an API client, a
cache, anything holding a connection or a rate limiter — will not hand-roll a singleton,
because nothing in `docs/modules-guide.md` says it has to. The author will not discover
the problem on a one-chain test setup. They will discover it when someone runs eight
chains and exhausts an API budget eightfold, which is exactly the failure this project
already hit once.

**The singleton ignores `cfg`, which contradicts the aliasing mechanism.** `get_module`
resolves `cfg["type"]`, so `[modules.low_altitude]` and `[modules.mid_altitude]` can both
be `altitude_filter` with different bounds. That works for filters because they are
constructed fresh. It does not work for `adsbdb` or `tar1090_db`: two differently
configured aliases would silently both receive whichever instance was built first.
Neither takes config today, so nothing is broken right now. It is a trap laid for the
first person who gives one of them a config option.

Pooling in the factory fixes both. Modules go back to being dumb, and the aliasing becomes
honest — same name and config gets one instance, different config gets its own.

---

## Change 1 — pool in `get_module()`

In `modules/__init__.py`, mirroring `storage/__init__.py`:

```python
import json
import threading

_INSTANCES: dict[tuple[str, str], BaseModule] = {}
_INSTANCES_LOCK = threading.Lock()


def _cfg_key(cfg: dict) -> str:
    """Stable, hashable representation of a module's config block."""
    return json.dumps(cfg, sort_keys=True, default=str)


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
            _INSTANCES[key] = module.get(cfg)
        return _INSTANCES[key]


def clear_module_pool() -> None:
    """Drop all pooled instances. For tests only."""
    with _INSTANCES_LOCK:
        _INSTANCES.clear()
```

Three points on the design:

**Key on the config block name.** `brief-config-strictness.md` made a `[modules.<name>]`
block mandatory for every module reference, so the rule is now simply: **one block, one
object.** That is predictable from reading `config.toml` without needing to know what any
`type` key resolves to.

The config forms the second half of the key so that editing a block's settings produces a
new instance rather than silently reusing one built from the old values — this matters if
the pool is ever cleared and rebuilt, and it makes the pool self-describing.

In the current config this gives: three `altitude_filter` objects (`low_altitude`,
`mid_altitude`, `upper_altitude`, `high_altitude` — four, in fact), one
`ground_distance_filter` per named block, and **one** `adsbdb`, **one** `tar1090_db`,
**one** `closest_filter` shared across all eight chains. The expensive things collapse
because every chain names the same block.

**The lock is held across construction**, as `get_storage` does. This matters: processor
chains build their modules from their own threads, so without it two threads can both
miss the cache and both construct — which for `tar1090_db` means two concurrent CSV
downloads and two SQLite builds. The cost is that a slow constructor blocks other module
construction, which happens once at startup and is the same trade `get_storage` already
makes.

**`default=str` on the JSON dump** handles TOML datetimes and anything else non-serialisable
without the key function being able to raise. Config values that stringify identically
collapse to one instance, which is correct.

---

## Change 2 — remove the hand-rolled singleton from `adsbdb`

Delete `_INSTANCE`, `_INSTANCE_LOCK`, and the `global` statement. `get()` becomes:

```python
def get(cfg: dict) -> AdsbdbEnricher:
    from config import config as squawk_config
    data_dir  = Path(squawk_config.squawk.data_dir)
    cache_dir = data_dir / "modules" / "adsbdb"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return AdsbdbEnricher(cache_dir=cache_dir)
```

`self._rate_lock` on the instance **stays**. It guards `_try_acquire()` against concurrent
calls from the eight chain threads that now share the instance — that requirement is
unchanged, and if anything the factory pooling makes it more clearly load-bearing.

---

## Change 3 — remove the hand-rolled singleton from `tar1090_db`

Delete `_SQLITE_INSTANCE`, `_SQLITE_LOCK`, and the `global` statement. `get()` keeps all
of its existing logic — refresh check, download, SQLite build, the `Tar1090DbEnricher(db={})`
no-op fallbacks — but assigns to a local rather than a module global and returns a fresh
enricher wrapping it. The `with _SQLITE_LOCK:` block goes; `_INSTANCES_LOCK` in the factory
now provides that serialisation.

`SQLiteTarDb`'s `threading.local()` connection handling **stays**. A pooled instance is
shared across the ingest thread and any chain thread that uses it, and per-thread SQLite
connections are how that stays safe.

---

## Change 4 — document the thread-safety requirement

Pooling makes a previously-implicit requirement explicit: **module instances are shared
across chains that run in separate threads.** A module holding mutable state must guard it.

Every module currently shipping is already fine — the filters are stateless, `adsbdb` has
`_rate_lock`, `tar1090_db` uses thread-local connections — but a contributor cannot infer
this from the existing docs.

In `docs/modules-guide.md`, add to the "Writing your own" checklist:

> **Instances are shared.** The factory returns one instance per `[modules.<name>]` block,
> however many chains reference it. Eight chains naming `adsbdb` share one object. Chains run in separate threads, so `process()` can be called concurrently on
> the same instance. Stateless modules need no special care. If you hold mutable state — a
> counter, a rate limiter, a cache — guard it with a lock. If you hold a resource that
> isn't thread-safe, such as a database connection, make it thread-local.

In `docs/modules-reference.md`, the `adsbdb` and `tar1090_db` entries should stop
describing the shared instance as a property of those modules and describe it as a
property of the factory.

---

## Change 5 — tests

**Pooling makes tests order-dependent unless they reset.** This is the main risk in the
change and needs handling before anything else.

`tests/test_module_adsbdb.py` already has a `reset_shared_instance` fixture that saves,
nulls and restores `adsbdb_module._INSTANCE`. Retarget it at `clear_module_pool()`.
`test_get_returns_shared_instance` and `test_rate_limiter_is_shared_across_get_calls`
should now go through `get_module("adsbdb")` rather than `adsbdb.get({})` — they are
testing the factory's guarantee now, not the module's.

`tests/test_tar1090_db.py:125` — `test_missing_csv_returns_noop_enricher` monkeypatches
`_download` and `data_dir` but never resets `_SQLITE_INSTANCE`. If an earlier test in the
same session populated it, this test passes for the wrong reason. **Verify whether it
currently does**, then make it call `clear_module_pool()` regardless. If it turns out to
be a real latent order-dependency, say so in the commit — it is worth knowing that it was
there.

Audit any other test calling `get_module` or `get_ingest_modules` for the same problem;
`tests/test_ingest_modules.py` already clears `storage._INSTANCES` directly, so the
pattern exists. Prefer an autouse fixture calling `clear_module_pool()` over scattering
individual calls.

New tests:

1. **Same name, same config, one instance** — two `get_module("closest_filter")` calls
   return the same object (`is`).
2. **Same block referenced repeatedly, one instance** — eight `get_module("adsbdb")`
   calls return one object, mirroring the eight TV-wall chains.
3. **Same type, different blocks, different instances** — `get_module("low_altitude", {...})`
   and `get_module("mid_altitude", {...})` are distinct objects with distinct bounds.
4. **Different types, both no config** — `get_module("closest_filter")` and
   `get_module("pass_through")` are distinct.
5. **List-valued config keys correctly** — `registration_filter`'s `registrations` list
   round-trips through `_cfg_key`; the same list gives one instance, a different list
   gives two.
6. **Nested config keys correctly** — a config block containing a sub-table produces a
   stable key across calls.
7. **Unknown module still raises** — `get_module("nonexistent")` raises `ValueError`, and
   nothing is left in the pool afterwards.
8. **`adsbdb` rate limiter is shared via the factory** — two `get_module("adsbdb")` calls
   return one object, so one `_call_times` deque.
9. **`clear_module_pool()` works** — instance before and after are distinct objects.

---

## Explicitly out of scope

- `get_display()`. It has its own sharing model — `display/http/__init__.py` pools
  servers by port while each chain keeps its own `HttpDisplay` for `chain_name` and panel
  title. Pooling displays would be a no-op there (the injected `chain_name` makes every
  cfg distinct) and is a separate question for the other three displays.
- The `from config import config` calls inside module `get()` functions. Still an open
  design question, unchanged by this brief.
- Any new module, filter, or layout.
- Any change to how ingestors obtain their modules — `get_ingest_modules()` calls
  `get_module()` and picks up the pooling for free.
