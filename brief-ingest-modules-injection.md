# Brief: build ingest modules in main.py, not in each ingestor

**Scope:** `main.py`, `ingestor/__init__.py`, `ingestor/personal_adsb/ingestor.py`,
`ingestor/concorde/ingestor.py`, `docs/primary_ingestor.md`, `docs/modules-guide.md`,
`tests/test_ingest_modules.py`, `tests/test_personal_adsb.py`, `tests/test_concorde.py`.

**Do not** change `storage/`, `processor/`, `display/`, `modules/`, `config.py`,
`config.toml`, or any module.

No config change. No behaviour change for any currently shipping configuration.

---

## Problem

`get_ingest_modules()` is a helper each ingestor must remember to call.
`personal_adsb` calls it. `concorde` does not.

So the invariant is not "everything in the pot has been through its configured modules" —
it is "everything from `personal_adsb` has." The distinction is invisible from the config,
which is where someone would look.

The cost shows up when the code changes. `brief-tar1090-to-ingest.md` had to carry an
explicit "do not modify concorde" instruction, because the enrichment being added applied
to one ingestor and not the other. Every future change to ingest-time behaviour pays that
tax again: two code paths, one exception, stated in prose rather than enforced anywhere.

A third ingestor — a FlightAware source, say — inherits the trap. Nothing in the code or
the docs tells its author that `get_ingest_modules()` exists or that skipping it silently
disables whatever the config asked for.

This is the same shape as two problems already fixed. `adsbdb` and `tar1090_db` hand-rolled
singletons that `get_module()` should have provided; four modules hand-rolled global config
access that `ModuleContext` should have provided. In both cases the fix was to move the
work up into the thing that already knew how to do it.

---

## Why not move enrichment to the write path

The obvious alternative — `save_aircraft_array()` runs a configured chain, so no ingestor
can skip it — was considered and rejected.

It would make enrichment a property of the installation rather than the source, and
per-source treatment is a thing this project actually wants. A handheld running a
FlightAware ingestor at an airport gets registration and type in the API response; it has
no use for `tar1090_db` and no reason to download `aircraft.csv.gz` and build a SQLite
index over mobile data to enrich fields that are already populated. Leaving `modules` off
that ingestor's config block is how you say so, and the write-path model has no way to
express it.

So the per-ingestor `modules` key stays. Only its construction moves.

---

## Change 1 — `main.py` builds the list

`main.py` already loops over enabled ingestors to start their threads. It gains the module
construction:

```python
from ingestor import get_ingest_modules

for name, cfg in config.ingestors.items():
    if not cfg.get("enabled"):
        continue
    module  = importlib.import_module(f"ingestor.{name}.ingestor")
    modules = get_ingest_modules(cfg)
    thread  = threading.Thread(
        target = module.run,
        args   = (modules,),
        daemon = True,
        name   = f"ingest-{name}",
    )
    thread.start()
```

Match the existing loop's actual structure — the above is illustrative, not a literal
replacement. The point is that `get_ingest_modules(cfg)` is called here, once per
ingestor, before the thread starts.

Building before the thread starts is deliberate: a module that fails to construct should
fail at startup with a visible traceback, not inside a daemon thread where it would be
swallowed.

---

## Change 2 — ingestors receive their modules

Both `run()` functions take the list as a required argument:

```python
def run(ingest_modules: list[BaseModule]) -> None:
```

**`personal_adsb`** — delete the `get_ingest_modules(cfg)` call and the import. The
existing loop is unchanged:

```python
for m in ingest_modules:
    aircraft = m.process(aircraft)
```

**`concorde`** — add the same loop before its `save_aircraft_array([aircraft])` call. With
no `modules` key in its config block the list is empty and the loop does nothing, so
behaviour is identical. But the carve-out is gone: concorde is no longer an exception, and
no future brief needs to mention it.

`get_ingest_modules()` stays in `ingestor/__init__.py` — it is still the right place for
the resolution logic, it just gets called from one level up. Update its docstring to say
it is called by `main.py`, not by ingestors.

---

## Change 3 — documentation

`docs/primary_ingestor.md` — the section describing how an ingestor obtains its modules
needs rewriting. An ingestor author's contract is now:

> Your `run()` receives a list of already-constructed modules. Apply them to your aircraft
> list immediately before saving:
>
> ```python
> for m in ingest_modules:
>     aircraft = m.process(aircraft)
> storage.save_aircraft_array(aircraft)
> ```
>
> The list comes from the `modules` key in your ingestor's config block, built by
> `main.py`. It may be empty. You do not call `get_ingest_modules()` yourself.

Check `docs/modules-guide.md` for any text implying an ingestor fetches its own modules.

---

## Change 4 — tests

`tests/test_ingest_modules.py` currently tests `get_ingest_modules()` directly. That still
works and should stay — the resolution logic is unchanged.

Update:

- `tests/test_personal_adsb.py` — any test invoking `run()` passes a module list. An empty
  list is the right default for tests not exercising enrichment.
- `tests/test_concorde.py` — same, plus a new test proving concorde now applies its
  modules: run it with a `pass_through` in the list and assert the saved aircraft came
  through it. This is the test that would have failed before this change, so it is the one
  that matters.

New:

- **`run()` requires the argument** — calling `run()` with no module list raises
  `TypeError`. This is what stops a future ingestor quietly defaulting to no modules.
- **Every ingestor's `run()` accepts it** — iterate `ingestor/` with `pkgutil`, import each
  `.ingestor` submodule, and assert `run` takes a parameter. Same shape as the existing
  `KEYS` and `ctx` contract tests, and the reason it exists is that this class of omission
  has now happened twice.

---

## Explicitly out of scope

- Moving, shrinking or removing the concorde ingestor. This brief removes the reason it
  was a maintenance burden; whether it stays is a separate decision.
- Any change to `save_aircraft_array()` or the storage layer.
- Per-source expiry. `_expire_stale` uses a single mtime-based TTL, which would matter for
  a slow-polling ingestor, but no such ingestor exists yet.
- Field-level merging between sources. `save_aircraft_array()` replaces whole records on
  `observed_at`; newest wins. That is the intended behaviour.
- Writing a FlightAware ingestor.
