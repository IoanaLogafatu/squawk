# Brief: negative-result cache for `vrs_route`

## Goal

A callsign genuinely absent from VRS's routes table gets re-queried,
re-printed, and re-logged **every single cycle**, forever, for as long as
that hex stays in the pot — because a failed lookup leaves every `route`
field `UNKNOWN`, which is indistinguishable from "never asked." The
carry-forward brief fixed this for successful lookups (a filled field
already skips re-querying); this brief fixes the mirror case: remembering
a *miss* long enough to stop repeating it.

Direct precedent already in the codebase: `adsbdb`'s `_not_found_marker()`
+ `_ROUTE_TTL_SECONDS = 3600` does exactly this for its own route lookups,
with the stated reasoning "a route is a property of today's flight."
Reuse that reasoning and that number rather than inventing a new one.

## Design

`VrsRouteEnricher` gains one new piece of state:

```python
_NOT_FOUND_TTL_SECONDS = 3600   # 1 hour — same as adsbdb's _ROUTE_TTL_SECONDS

self._not_found: dict[str, float] = {}   # callsign|hex key -> time.time() when confirmed absent
self._not_found_lock = threading.Lock()  # module instances are pooled; match existing thread-safety convention even though today's wiring is single-threaded ingest
```

Two categories, both need this, keyed differently:

- **`unknown_callsign`** (callsign present, no matching row) — key by
  `callsign`. A given callsign is genuinely a property of one flight; if
  VRS doesn't have it now, it won't gain it in the next 5 seconds.
- **`no_callsign`** (aircraft transmits no `flight` field at all) — key by
  `icao_hex`. Not a VRS miss at all, just an aircraft that structurally
  doesn't broadcast a callsign (common for GA/military at low altitude),
  but the same "don't re-log every cycle" problem applies.

### On each aircraft, before doing anything else

```python
key = callsign or f"hex:{icao_hex}"
with self._not_found_lock:
    last = self._not_found.get(key)
if last is not None and time.time() - last < _NOT_FOUND_TTL_SECONDS:
    continue   # skip entirely: no DB call, no print, no unresolved-log write
```

This is a **complete skip**, not a quieter version of the existing path —
none of the three effects below fire while a key is within its TTL
window:

1. **No `get_route()` call.** The actual point of this brief — stop
   asking SQLite the same already-answered question every cycle.
2. **No console line**, at any `log_level`. The original complaint (the
   scrolling repeat block) was fundamentally this: the same miss printed
   itself every cycle forever. Suppressing it here fixes that directly,
   for `verbose` and `errors` alike — `errors` was never meant to mean
   "print every miss forever," just "print a miss when it happens."
3. **No `unresolved.jsonl` append.** That log exists to measure VRS's
   real coverage gap — a file with the same still-missing callsign
   appended every 5 seconds for hours doesn't measure anything more than
   one clean entry would, it just makes the file harder to read and
   grow without bound. One entry per miss per TTL window is the correct
   granularity for what that log is for.

### On a genuine miss (first time, or TTL expired)

Record it and proceed exactly as today — call `get_route()`, get nothing
back, print the line (respecting `log_level`), write the unresolved-log
entry:

```python
with self._not_found_lock:
    self._not_found[key] = time.time()
```

### On a hit

If `key` was previously in `self._not_found` (a route that used to be
missing has now appeared — plausible after the weekly `vrs_standing_data`
refresh), remove it. Don't leave a stale negative entry sitting alongside
a fresh positive result.

### Cache growth

`self._not_found` grows for as long as the process runs, one entry per
distinct callsign/hex ever seen missing. Not unbounded in practice —
bounded by however many distinct aircraft pass through in a `_TTL`
window — but worth a simple sweep, same shape as `adsbdb._sweep()`: drop
entries older than `_NOT_FOUND_TTL_SECONDS` opportunistically each cycle,
rather than letting it grow across the life of a long-running process.

## Tests

New cases in `tests/test_vrs_route.py`:

1. First miss on a callsign: DB queried once, printed once (at `errors`/
   `verbose`), one `unresolved.jsonl` line written.
2. Same callsign, next cycle, still within TTL: **zero** further DB
   calls, zero print lines, zero log lines — assert absence on all three,
   not just "no new field written."
3. Same callsign, TTL expired: miss is re-attempted in full, as if fresh.
4. A callsign that was missing and then appears in a hit (simulate a
   dataset refresh returning a real route): the not-found entry is
   cleared, the hit is applied and logged normally.
5. `no_callsign` case follows the same three-effect suppression, keyed by
   hex instead of callsign.
6. Two different callsigns both missing don't interfere with each
   other's TTL windows — independent cache entries.
7. Cache sweep drops expired entries rather than growing unbounded across
   many cycles (a long-running-process test, same shape as whatever
   `adsbdb`'s own `_sweep()` test already proves).

## Docs

`docs/modules-reference.md`'s `vrs_route` entry — add the negative-cache
behavior and its 1-hour TTL, explicitly cross-referencing `adsbdb`'s
identical pattern so a future reader recognizes it as a repeated
convention rather than a one-off. Note in passing (not a fix, just a
flag) that `adsbdb`'s own `unresolved.jsonl` has the same
append-forever-for-a-repeat-miss behavior this brief fixes for
`vrs_route`, in case `adsbdb` is revisited later.
