# Data Sources — Developer Guide

A **data source** is an external dataset that more than one module may need: one download, one freshness check, one instance — however many modules name it.

Modules transform aircraft. Data sources don't: they hold a file on disk and keep it current. A module that needs one names it in config and asks the context for it.

```
[ data_sources ] ← named by → [ modules ] → [ displays ]
```

## Why they exist

Two modules reading the same dataset is the whole problem. Without shared infrastructure, each one owns a private copy: two downloads of the same file, two staleness clocks, two caches on an SD card — and, when both refresh at once, two writers racing over one path.

`get_data_source()` pools instances by `(name, cfg)`, exactly as `get_module()` does. One `[data_sources.<name>]` block always resolves to one object, so "one download" is structurally true rather than merely intended.

## Configuration

A source is declared in its own top-level section, structurally parallel to `[ingestors.*]` and `[modules.*]`:

```toml
[data_sources.vrs]
type = "vrs_standing_data"    # resolves to data_sources/vrs_standing_data.py
```

`type` names the implementation. Every other key is free-form and validated by that type's own `KEYS`.

A module opts in by naming the source:

```toml
[modules.vrs_route]
source = "vrs"

[modules.vrs_aircraft]
source = "vrs"
```

Both modules above share one `vrs` instance, and therefore one download.

`source` is recognised on **any** module block, whatever its type — like `type`, it is handled once at the factory level, so no module lists it in its own `KEYS`.

### Every named source needs a block

A module naming `source = "vrs"` with no `[data_sources.vrs]` block is rejected at startup, naming both — the same rule, and the same reasoning, as a chain naming a module with no `[modules.*]` block. A typo becomes a startup error rather than a module that quietly enriches nothing.

An unreferenced `[data_sources.*]` block warns and is ignored, like an unreferenced module block.

## When to use one — and when not to

Use a data source when a dataset is, or is about to be, read by more than one module.

Managing a download privately inside one module is **not wrong**. `tar1090_db` does exactly that — it downloads its CSV, checks its own age, and rebuilds its own SQLite index, all inside the module. It has not been migrated onto this infrastructure, and that is a judgement about priority, not a verdict on the pattern: it works, it is tested, and nothing else reads its CSV today.

The line is roughly:

- **One module, no second in sight** — private is fine. Fewer moving parts, and everything the module needs is in one file.
- **Two modules today** — use a source. The alternative is two downloads of one file.
- **One module today, a second expected soon** — usually still worth it. Retrofitting a shared source under a module that already owns its download means moving the files on disk, which is more disruptive than starting shared.

## Writing a source type

```python
# data_sources/vrs_standing_data.py

from data_sources import BaseDataSource, DataSourceContext

class VrsStandingData(BaseDataSource):

    def __init__(self, cfg: dict, ctx: DataSourceContext) -> None:
        self._dir = ctx.source_dir
        ...

    def ensure_fresh(self) -> None:
        ...        # cheap; refreshes only when this type's policy says to

    @property
    def directory(self) -> Path:
        return self._dir

KEYS = {"refresh_days"}

def get(cfg: dict, ctx: DataSourceContext) -> VrsStandingData:
    return VrsStandingData(cfg, ctx)
```

1. **Subclass `BaseDataSource`** and implement `ensure_fresh()` and `directory`.
2. **Expose `get(cfg, ctx) -> BaseDataSource`** — the same factory shape modules use.
3. **Declare `KEYS`** with your own options only. `type` is recognised for you.
4. **Write only inside `ctx.source_dir`**, and create it when you first write. The factory does not create it.

`data_sources/vrs_standing_data.py` is the first worked example of a concrete `BaseDataSource` and is worth reading end to end: a whole-repo zip download (temp-and-replace, same failure-safety concern as `DiskDriveStorage`'s writes), eight tables loaded into one SQLite file in one build pass, and `ensure_fresh()` gating that behind the elapsed-days policy above.

### The `ensure_fresh()` contract

```python
def ensure_fresh(self) -> None:
    """Check staleness and refresh if needed."""
```

**Cheap and safe to call often.** A module may call it on every cycle — every five seconds, forever — and is *encouraged* to, because that is what lets a long-running process pick up a new dataset without a restart. Gate the real work behind your own interval so that most calls do nothing but compare two numbers.

**Idempotent.** Ten calls in a row must produce one download, not ten.

**Refresh policy is yours.** `BaseDataSource` deliberately does not prescribe one:

| Source | Policy |
|---|---|
| tar1090 CSV | Stale after N days; check the age periodically |
| VRS standing data | Stale after N days (default 7), checked against the built DB file's own mtime |

These two look alike — both are elapsed-days thresholds — and that similarity is itself worth flagging rather than assuming: `vrs_standing_data` was originally scoped with a "published once daily at a known hour, have I got today's yet" policy (routes/airports only, matching VRS's daily publish cadence). Once the source grew to load the entire repo — eight tables in one build — that policy was dropped in favour of reusing tar1090's simpler elapsed-days shape, on the judgement that a week of headroom is enough for a dataset this size. The two sources reached the same policy *shape* independently, not because one was copied from the other's contract — a future source with a genuine "once daily at a known hour" publish cadence should still implement that shape itself, the way this one very nearly did.

**Don't raise.** A source that throws from `ensure_fresh()` takes its caller's cycle with it. A failed download with a usable cached file on disk is a warning, not an exception.

## Using one from a module

```python
def get(cfg: dict, ctx: ModuleContext) -> VrsRoute:
    source = ctx.data_source(cfg["source"])
    return VrsRoute(source)
```

`ctx.data_source(name)` returns the shared instance for that block. Config has already guaranteed the block exists, so this does not need a guard.

Then call `ensure_fresh()` before reading — from `process()`, not just from `get()`:

```python
def process(self, aircraft):
    self._source.ensure_fresh()
    ...
```

Calling it only at construction means a process that runs for months never picks up a new dataset. That exact bug is why `ensure_fresh()` is specified as cheap: there is no reason not to call it every cycle.

A module with no `source` key never calls `ctx.data_source` at all.

## Directories

A source's files live under its **block name**:

```
<data_dir>/data_sources/<source_name>/
```

This is the opposite of a module's `ctx.module_dir`, which is keyed on module *type* so that two blocks aliasing one module share a cache. For sources the reverse is right: two blocks of the same type are two different datasets — a different region, a different feed — and must not overwrite each other's files.

Sources also sit outside `<data_dir>/modules/` entirely, because a source belongs to no single module.

## Instances are shared — guard mutable state

The factory returns one instance per block. Modules referencing it run in separate threads, so `ensure_fresh()` can be called concurrently on the same object. If your refresh mutates state — a last-checked timestamp, a handle to a rebuilt index — guard it with a lock, or make the resource thread-local. This is the same rule modules follow, for the same reason; see the Modules guide.

## Testing

- Register a fake type under `data_sources.<name>` in `sys.modules` rather than shipping a no-op source; `tests/test_data_sources.py` shows the pattern.
- Prove pooling with object identity: two modules naming one source must get the *same* object.
- Prove `ensure_fresh()` is idempotent with a counter, then advance a test-driven clock past the staleness window and prove it fetches again.
- Call `clear_data_source_pool()` between tests — pooled instances otherwise leak across them.
