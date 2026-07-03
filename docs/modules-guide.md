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

The chain is an ordered list under `[[processor.chain]]` in `config.toml`. Order is meaningful:

```toml
[[processor.chain]]
name = "route_enrichment"
min_altitude_feet = 15000

[[processor.chain]]
name = "closest_filter"

[[processor.chain]]
name = "epaper_display"
```

Each table is one chain entry. Your factory receives the table as `cfg`.

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
2. **Add the factory:** `def get(cfg: dict) -> YourPlugin`.
3. **Always return a list.** Empty if needed. Never `None`.
4. **Guard `UNKNOWN`.** Any field on any aircraft can be `None`. Plan accordingly.
5. **Document your category.** A docstring saying "this is a filter / enrichment / display" helps the next person reading config.
6. **Long-lived resources in `__init__`.** API clients, caches, file handles — build once, reuse across cycles.
7. **Don't raise.** A module that crashes takes the cycle with it. Wrap risky I/O.
8. **For enrichments: cache first, API second.** Bound your external traffic.
9. **For enrichments: respect predicates.** A skipped lookup costs nothing.
10. **For filters: empty-list behaviour is meaningful.** Returning `[]` is fine; just make sure downstream modules handle it (they should).

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
