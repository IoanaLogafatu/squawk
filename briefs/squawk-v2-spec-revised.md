# Squawk v2 Specification (Revised)

Squawk is modular software designed to extract data from aircraft trackers
and build custom displays tailored to exact requirements. Mixing and
matching components makes it easy to assemble a bespoke interface.

## Module types

Each module type lives in its own folder, with a consistent naming
convention:

- **Ingest** — grabs tracking data from a source and hands it into a chain.
- **Transform** — filters and/or enriches the data as it passes through a
  chain.
- **Snapshot** — holds the current status of tracked flights, continually
  updated.
- **Output** — the final destination of the data: a screen, e-paper
  display, text message, database, or log.
- **Service** — an always-online process maintaining its own shared state
  file(s) under `<data_dir>/services/<name>/`, refreshed on its own
  schedule (e.g. every second, or whenever it has fresh data). Normally
  paired one-to-one with the module that uses it (a FlightAware API service
  backing a FlightAware transform; a Concorde flight simulator backing a
  Concorde ingestor). Its purpose is to prevent duplicate work: several
  instances of a module that would otherwise each look up the same thing
  independently instead share one service's file. Consuming modules only
  ever *read* that file — they never call the service process directly.

## Processing chains

A processing chain pairs a core module (ingest / snapshot / output) with a
list of transform modules. There are three chain families:

- **Ingest chain** — runs its ingest module, *then* passes the result
  through its transform list. (There's no data to transform beforehand —
  the ingest module is the one creating it.)
- **Snapshot chain** — runs its transform list *first*, then hands the
  result to the snapshot module. Only one snapshot chain may be defined.
- **Output chain** — runs its transform list first, then acts on the
  snapshot according to its `source` setting (see below). May be defined
  multiple times — one instance per real destination.

**Processing order is fixed by chain type, not configurable per-chain**:
ingest is core-then-transform; snapshot and output are transform-then-core.

Ingest and output chains may each have multiple named instances.

```
[Ingest chain] → [Snapshot] → [Output chain]
                              → [Output chain]
                              → [Output chain] ...
```

### Output chain `source`

Each output chain declares what it watches:

- **`source = "live"`** — polls the snapshot on `poll_interval_seconds`,
  runs the transform list, calls the output module's `send()` with the
  current aircraft list. This is the "what's up right now" feed.
- **`source = "deletions"`** — never touches the live snapshot. Watches its
  own deletion cursor (see Snapshot behaviour) and calls the output
  module's `on_deleted()` once per aircraft that expires.

**An output chain has a single input, decided by its `source`.** A
destination that cares about both live state and departures — Pushover
being the obvious real example — is configured as two chain instances of
the same module: one `source = "live"`, one `source = "deletions"`. This is
exactly the `display`/`history` pattern already used for `console`, just
applied to any output module, not console specifically. Each instance only
ever receives one kind of data, so there's no double-sending to guard
against.

## Data structure

Data passes between modules as standardised JSON — one object per aircraft.
Even where a module only ever handles a single aircraft, data is passed as
an array, for compatibility with modules that handle many at once.

```
airport = {
    "iata": str, "icao": str, "name": str,
    "municipality": str, "country": str,
}

airline = {
    "airline_name": str, "airline_iata": str,
    "airline_icao": str, "airline_country": str,
}

airframe = {
    "registration": str, "type_code": str,
    "type_description": str, "manufacturer": str,
    "operator": str,
}

meta = {
    "icao_hex": str, "squawk": str,
    "first_seen": datetime, "last_seen": datetime,
}

location = {
    "latitude": float, "longitude": float,
    "altitude_feet": float,
}

direction = {
    "ground_speed_knots": float, "heading": float,
    "vertical_rate_fpm": float,
}

route = {
    "callsign": str, "flight_number": str,
    "origin": airport, "destination": airport,
}

aircraft = {
    "meta": meta, "location": location,
    "direction": direction, "route": route,
    "airframe": airframe, "airline": airline,
    "raw": object,
}

raw = {
    module_key: object
}
```

`raw` holds each contributing module's own data, keyed by that module's
name (e.g. `raw.concorde_A`, `raw.adsbwest`) — never a single shared
value. This is what lets several sources report on the same aircraft
without overwriting each other. `meta` deliberately does **not** carry a
"which source touched this last" field — see Snapshot behaviour for why.

## Snapshot behaviour

The snapshot module maintains the current set of visible aircraft. The
initial implementation is folder-based, one file per aircraft, named
`ICAO_HEX.json`.

Each incoming aircraft record is merged into the existing one (if any)
using **three independent rules**, not one blanket rule:

1. **`raw[source_key]`** — written unconditionally, whenever that module
   reports. No date check. The incoming module's own key is overwritten;
   every other module's key in `raw` is left untouched.
2. **`location`, `direction`, `last_seen`** — gated by `last_seen`. Only
   accepted if the incoming record's `last_seen` is newer than what's
   already stored. This is the "one source wins at a time" group — a
   position report only makes sense from whichever source saw it most
   recently.
3. **Everything else** (`route`, `airframe`, `airline`, `meta.squawk`,
   `meta.icao_hex`) — no `last_seen` gate. If the incoming value has data,
   it overwrites whatever's currently stored, even if something was already
   there. If the incoming value is blank, it's skipped and the existing
   value is left alone. Note this is **not** "fill only if blank" — a
   later, more complete report is allowed to replace an earlier one, on the
   understanding that two disagreeing sources for the same field would flap
   between values with no tie-break beyond "whichever wrote most recently
   with real data." Acceptable for now; revisit if a real conflicting
   source ever makes this a problem in practice.

If no record exists yet for the incoming `icao_hex`, the snapshot chain's
own transform list runs on the whole incoming batch first, then the record
is created outright (all three rules above are just "write everything," since
there's nothing to merge against).

Use atomic writes (write to a temp file, then rename) for every file write —
multiple ingest chains can call `save()` for the same hex within moments of
each other. Guard the whole read-merge-write sequence with a per-file lock,
not just the final write — the merge logic above only gives the right answer
if that sequence is atomic.

**Expiry** — a background loop, its own thread separate from anything
handling incoming writes, wakes every `scan_interval_seconds` and deletes
any aircraft whose `last_seen` exceeds `expiry_minutes`. Each deletion is
written as a notice into `<data_dir>/snapshot/_deletions/`. Each output
chain with `source = "deletions"` keeps its own cursor file
(`<data_dir>/output/<name>/deletions_cursor.txt`) so every chain sees every
deletion exactly once, in order, regardless of its poll rate, and a
restart resumes rather than replaying already-seen notices. Deletion
notices are pruned after `deletion_retention_minutes` (must comfortably
exceed the slowest deletions-watching chain's poll interval).

## Module self-optimisation

Modules are expected to do the work of avoiding unnecessary computation
themselves: caching lookups in their own memory or disk, tracking last-seen
or last-updated state, and only passing on aircraft that have actually
changed. The chain and snapshot handle correctness; keeping chains free of
redundant work is each module's own responsibility.

## Logging

Each module accepts a `debug_level` in its config block
(`error` | `warn` | `info` | `debug`, default `warn`). Use Python's stdlib
`logging` module, one logger per module instance, named after its config
key. `debug` level logs every service read and merge/skip decision a module
makes.

## Cache folders

Every service and every module instance gets its own subfolder, keyed by
its config name:

```
<data_dir>/services/<name>/
<data_dir>/modules/<name>/
```

This guarantees two instances — or a service and a module — never collide
on disk.

## Run profiles

A profile names a set of chains to launch together as one command, purely
as a launcher convenience — it does not change how chains run; each is
still spawned as its own independent OS process.

```toml
[run.concorde_test]
chains = [
    "ingest_chain.concorde_A",
    "ingest_chain.concorde_B",
    "snapshot_chain.snapshot_chain",
    "output_chain.display",
    "output_chain.history",
]
```

`python main.py run concorde_test` spawns each listed chain as a subprocess.
**If any one chain in the profile exits or crashes, the launcher kills every
other chain in the profile** — a profile is meant to be watched as one
running unit, not left partially alive with one member silently dead.
Ctrl-C on the launcher forwards the signal to all children so the whole
profile stops together.

Profiles live in `config.toml` alongside chain definitions — not a separate
file — since they're still config, just describing a different kind of
thing.

## Initial setup

The requirement is a base program that, in theory, never needs updating
again — new features arrive as additional modules, not core changes.

For the first build:

- A `concorde` **service**, running continuously as its own process,
  simulating Concorde flying a circuit around the observer (climb from
  2,000ft at spawn to 12,000ft overhead, mirrored descent to 2,000ft at
  despawn; constant 300kt; 100nm pass, ~20 minutes). It writes its computed
  position to `<data_dir>/services/concorde/state.json` roughly once a
  second. Being the sole writer, it needs no lock-based coordination on the
  read side — ingestors just read whatever's currently there.
- Two `ingest_concorde` ingest chains (`concorde_A`, `concorde_B`), each
  reading the concorde service's state file at its own random 5–20s
  interval and merging into the snapshot — proving two independent readers
  landing on the same record without conflict, since both are reporting on
  the same underlying flight.
- The snapshot module is the real `file_object` implementation.
- Two output chains, both `output_console`: `display` (`source = "live"`)
  and `history` (`source = "deletions"`) — proving both output modes work.
- All transforms are `nop`.
- A `run.concorde_test` profile launching all six processes with one
  command.

```toml
data_dir = "data"

observer_latitude = 52.00
observer_longitude = -1.00

services = ["concorde"]

[snapshot_chain]
type = "file_object"
transform = ["nop"]
expiry_minutes = 5
scan_interval_seconds = 30
deletion_retention_minutes = 60
debug_level = "info"

[ingest_chain.concorde_A]
type = "ingest_concorde"
transform = ["nop"]
poll_min_seconds = 5
poll_max_seconds = 20
debug_level = "info"

[ingest_chain.concorde_B]
type = "ingest_concorde"
transform = ["nop"]
poll_min_seconds = 5
poll_max_seconds = 20
debug_level = "info"

[output_chain.display]
type = "output_console"
transform = ["nop"]
source = "live"
poll_interval_seconds = 5
debug_level = "info"

[output_chain.history]
type = "output_console"
transform = ["nop"]
source = "deletions"
debug_level = "info"

[run.concorde_test]
chains = [
    "ingest_chain.concorde_A",
    "ingest_chain.concorde_B",
    "snapshot_chain.snapshot_chain",
    "output_chain.display",
    "output_chain.history",
]
```

Console output must include a `Source` column, derived by listing `raw`'s
keys at display time (e.g. `concorde_A, concorde_B`) — not a stored field.

---

## Changes since the previous draft

This revision corrects and extends the version Opus built the current
codebase from. Everything below needs applying as a follow-up change, not
assumed already present:

1. **`last_seen` added as a third always-overwrite field**, alongside
   `location`/`direction`. The original list of two was an omission — the
   upsert decision itself depends on comparing `last_seen`, which only
   works if it's always current.
2. **`raw` now merges by key, not wholesale.** Previously specified (and
   implemented) as one of the "always overwrite" fields, meaning the last
   writer erased every other source's contribution. Now: each module's own
   key is written unconditionally; every other key is left alone.
3. **The merge rule for identity/fixed fields changed** from "fill only if
   the existing value is blank" to "overwrite whenever incoming has data,
   skip if incoming is blank" — no longer frozen after first write.
4. **`meta.ingest_source` is removed entirely** (it existed in the
   implementation but was never in this spec). Superseded by `raw`'s
   per-key structure, which already preserves which sources have reported
   without needing a separate "last source" field.
5. **Output chains gain a `source` setting** (`live` | `deletions`),
   determining whether the chain polls the live snapshot or only reacts to
   deletion notices. Same module can implement both behaviours (`send()`
   and `on_deleted()`); the chain config decides which is invoked.
6. **An output chain has a single input, per its `source`** — a
   destination wanting both live and deletion behaviour (e.g. Pushover)
   is two chain instances of the same module, not one module handling
   both hooks internally.
7. **Service definition corrected.** No longer "a locked state file, not a
   process" — services are real always-on processes that maintain a shared
   state file, single-writer, multi-reader. This reverses an earlier
   decision made mid-project; the earlier "no process needed, pure function
   over a lock" design for Concorde is dropped in favour of this.
8. **Concorde rejoins the process list** — six processes for the initial
   setup, not five.
9. **Run profiles added** — a `[run.<name>]` config table listing chains to
   launch together via one command. If any chain in a profile dies, the
   launcher kills the rest.
10. **Console output requires a `Source` column**, computed from `raw`'s
    keys, not stored.
