# Modules — Developer Guide

A **module** is a transform over a list of `Aircraft` objects. The processor holds an ordered chain of modules; on each cycle, it hands the source aircraft list to the first module and passes the output of each to the next.

Modules are how Squawk does everything between "aircraft observed" and "aircraft displayed": filtering down to what matters, enriching with external data, and rendering to screens. All three share one interface — the processor doesn't distinguish a filter from an enrichment from a display.

This guide covers writing any module. For display-specific concerns (background threads, hardware throttling) see the dedicated **Display Modules** guide.

## What makes a class a "module"

A module:

- Subclasses `BaseModule`.
- Implements `process(aircraft) -> aircraft`.
- Exposes a module-level `get(cfg) -> BaseModule` factory.
- Always returns a list — empty is fine, `None` is not.
- May mutate the aircraft objects it receives (unlike repositories).

The processor calls `process()` synchronously, in chain order:

```python
aircraft = source_aircraft
for module in chain:
    aircraft = module.process(aircraft)
```

The list flowing down the chain is a single conversation. Each module sees what the previous one produced.

## Configuration

Processor chains are configured under `[processors.<name>]` in `config.toml`, with an ordered list of module names. Order is meaningful:

```toml
[processors.screen]
enabled               = true
poll_interval_seconds = 5
modules               = ["tar1090_db", "adsbdb", "closest_filter"]
display               = "epaper"
```

Each module's specific settings are defined under `[modules.<module_name>]`.

### Every referenced module needs a block

Every name a chain or ingestor lists under `modules` must have a matching `[modules.<name>]`
table in config, even if the module takes no options — an empty table is normal and expected:

```toml
[modules.closest_filter]
```

One block means one instance. A name with no block is rejected at startup rather than
silently falling through to defaults — this is what catches a typo in a module name before
it quietly turns into a no-op filter.

### Ingestor modules — what enters storage

Modules listed on an ingestor define what enters storage. They run against every aircraft before it is saved, and every processor chain sees only what survives them — no chain can recover what was dropped.

Enrichment here is cheap and applies once for the whole installation, which is why `tar1090_db` belongs on the ingestor rather than in each chain.

A filter here is a deliberate choice to narrow the entire installation. A 20nm range filter is a reasonable configuration if you only care about aircraft visible from the window: storage stays small and every chain downstream is cheaper. Use it when that is what you mean. If you want a narrower view for a single panel, put the filter in that chain instead — a filter written on the ingestor will silently narrow all of them.

```toml
[ingestors.personal_adsb]
enabled = true
modules = ["tar1090_db"]      # runs once per aircraft on ingest
receivers = [ ... ]
```


## Categories — what modules do

By convention, modules fall into one of three patterns. The interface is identical; the patterns differ in what they do with the list.

- **Filter** — reduces or reorders the list. Output length ≤ input length. Example: `closest_filter`.
- **Enrichment** — fills in `UNKNOWN` fields. Output length = input length; individual objects are richer. Example: `route_enrichment` (planned).
- **Display** — writes the list to an external sink (screen, web page, log). Returns the list unchanged. Covered in detail in the Display Modules guide.

You can also write a no-op for use as a placeholder — see `pass_through`.

## Worked examples

### pass_through — the minimal case

```python
class PassThrough(BaseModule):
    def process(self, aircraft):
        return aircraft


def get(cfg: dict) -> PassThrough:
    return PassThrough()
```

The full shape of a module in two lines of logic. Useful as a reserved chain slot during development, or to verify the processor is wired up before any real module exists.

### closest_filter — a filter

```python
class ClosestFilter(BaseModule):
    def process(self, aircraft):
        candidates = [a for a in aircraft if a.dynamic.distance_nm is not None]
        if not candidates:
            return []
        return [min(candidates, key=lambda a: a.dynamic.distance_nm)]
```

Things to notice:

- **`UNKNOWN` candidates excluded explicitly.** `distance_nm` is `None` until it's known. Filtering before checking would crash on the comparison.
- **Return `[]` for "nothing qualifies"**, not `None`. Downstream modules iterate and crash on `None`.
- **No state.** Filters are typically stateless — they make a decision from the data in front of them and nothing else.

### altitude_filter — an altitude bounds filter

Filters aircraft by altitude bounds (`above` and `below`, inclusive `>=` and `<=`). Supports selecting `altitude_source` (`"alt_baro"` or `"alt_geom"`) and configurable `fallback` (default `True`).

```toml
[modules.altitude_filter]
above           = 10000        # Minimum altitude (feet)
below           = 30000        # Maximum altitude (feet)
altitude_source = "alt_baro"   # "alt_baro" or "alt_geom"
fallback        = true         # Fall back to alternate source if chosen source is missing
```


### route_enrichment — an enrichment (planned)

No code yet; this is the shape. The module looks up callsigns against adsbdb and fills in origin / destination / flight number where currently `UNKNOWN`. A SQLite cache means repeated lookups for the same callsign hit the API once.

```python
class RouteEnrichment(BaseModule):
    def __init__(self, cfg: dict) -> None:
        self._cache        = RouteCache(...)
        self._min_altitude = cfg.get("min_altitude_feet", 0)

    def process(self, aircraft):
        for a in aircraft:
            if a.session.origin_iata is not None:
                continue                               # already populated
            if (a.dynamic.altitude_feet or 0) < self._min_altitude:
                continue                               # predicate skip
            route = self._cache.lookup(a.session.callsign)
            if route is not None:
                a.session.origin_iata      = route.origin
                a.session.destination_iata = route.destination
                a.session.flight_number    = route.flight_number
        return aircraft
```

The pattern for enrichments:

- **`UNKNOWN` is the trigger.** If `origin_iata` is `None`, this module tries to fill it. If it's already a string, the module leaves it alone. Re-runs are idempotent.
- **Mutation in place.** Enrichments fill fields on the existing objects; they don't construct replacements.
- **Predicate filtering inside the module.** A `min_altitude_feet` cutoff skips lookups for low-altitude traffic (circuits, ground vehicles), bounding the daily API budget.
- **Cache first, API second.** The cache is hit before the API. Construct the cache in `__init__`, not per-cycle.

## The chain is ordered — order matters

The processor runs modules in the order they appear in config. There's no validation that the order is sensible. A few real cases:

- **Enrichments before filters that depend on them.** `closest_filter` operates on `distance_nm`. If the ingestor doesn't provide it, a distance-enrichment module has to come first.
- **Filters before expensive enrichments.** If you only care about the closest aircraft, filter first and run route lookup on one aircraft instead of fifty.
- **Displays after everything else.** Otherwise the display shows pre-filtered, pre-enriched data.

Today, getting the order right is the configurer's responsibility. A planned **dependency declaration** system (`REQUIRES`, `PREFERS`, `PRODUCES` metadata on each module, validated at startup) will catch these errors before they reach runtime.

## Writing your own — checklist

1. **Subclass `BaseModule`** and implement `process(aircraft)`.
2. **Add the factory:** `def get(cfg: dict) -> YourModule`.
2a. **Recommended: declare `KEYS`.** A module-level `KEYS = {"type", "your_option", ...}` set
    lets `get_module()` warn when a config block has a key it doesn't recognise — catches a
    misspelled option (`belwo = 5000`) that would otherwise fail silently. Not required; a
    module with no `KEYS` is simply not checked.
3. **Always return a list.** Empty if needed. Never `None`.
4. **Guard `UNKNOWN`.** Any field on any aircraft can be `None`. Plan accordingly.
5. **Document your category.** A docstring saying "this is a filter / enrichment / display" helps the next person reading config.
6. **Long-lived resources in `__init__`.** API clients, caches, file handles — build once, reuse across cycles.
7. **Don't raise.** A module that crashes takes the cycle with it. Wrap risky I/O.
8. **For enrichments: cache first, API second.** Bound your external traffic.
9. **For enrichments: respect predicates.** A skipped lookup costs nothing.
10. **Instances are shared.** The factory returns one instance per `[modules.<name>]` block,
    however many chains reference it. Eight chains naming `adsbdb` share one object. Chains
    run in separate threads, so `process()` can be called concurrently on the same instance.
    Stateless modules need no special care. If you hold mutable state — a counter, a rate
    limiter, a cache — guard it with a lock. If you hold a resource that isn't thread-safe,
    such as a database connection, make it thread-local.
11. **For filters: empty-list behaviour is meaningful.** Returning `[]` is fine; just make sure downstream modules handle it (they should).

## Testing

Modules are easy to test because `process()` is essentially pure:

- Construct the module with a config dict.
- Hand it a list of `Aircraft` objects (real or built in-test).
- Assert on the output: what was filtered, what was enriched, what was preserved.
- Hand it `[]`. Assert it returns `[]` and doesn't crash.
- Hand it aircraft with various `UNKNOWN` fields. Assert nothing crashes.

For modules that hit external services, mock the client at construction time. Pass a fake in via cfg, or use dependency injection at test time.

## Common pitfalls

- **Returning `None` instead of a list.** The single most common module bug. Always `return aircraft` (or `return []`).
- **Crashing on `UNKNOWN`.** Comparison with `None`, arithmetic on `None`, string formatting `None` — all common. Guard explicitly.
- **Hitting the API without a cache.** Every cycle, for every aircraft, forever. You will get rate-limited.
- **Doing slow work in `process()`.** Anything that waits — a network call, a screen draw — holds up the next cycle. Push slow work to a background thread and have `process()` poke shared state. See the Display Modules guide for the pattern.
- **Mutating in a filter.** Filters reduce; they don't enrich. If you're filling fields inside a filter, split it into two modules.
- **Assuming you're first or last in the chain.** Document what you expect upstream and downstream. The next person to add a module will appreciate it.
