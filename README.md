# Squawk

Squawk watches the sky above one place and does something useful with what
it finds. Aircraft are observed by **ingest chains**, merged into a shared
**snapshot**, and shown by **output chains** — each chain an independent OS
process, each doing one job.

This is the base build. Everything real is still to come: the only source
is a simulated Concorde, the only display is the console, and the only
transform does nothing. What it proves is that every module type and every
chain type work together, so adding a feature later means writing a module
and adding a line to `config.toml` — not changing the core.

## Everything is a file

There is nothing to stand up, no port to open, and no start-up order to
respect. Every process talks to every other process through files under
`data_dir`:

| Hand-off | How |
|---|---|
| Service → ingest | The service process publishes `data/services/<name>/state.json`; ingest modules read it. |
| Ingest → snapshot | The ingest process merges into `data/snapshot/` itself, in-process. |
| Snapshot → output | A `live` output process reads `data/snapshot/*.json` on its own poll. |
| Aircraft expired | The snapshot process leaves a notice in `data/snapshot/_deletions/`, which `deletions` output processes pick up. |

So "the snapshot" is a folder plus a merge rule that every writer applies
identically. A **service** is a real, always-on process, but nothing ever
calls it: it is the only writer of its state file, and the modules that
need its answer just read the file. A chain started on its own works
perfectly well with nothing else running — it just has nobody to talk to.

## Getting started

```bash
git clone <this repo>
cd Squawk
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp config.toml.example config.toml
$EDITOR config.toml          # at minimum, set observer_latitude/longitude
```

`config.toml` is gitignored; `config.toml.example` is the reference. Config
validation is strict — an unknown key, module type, transform or service
stops Squawk at start-up with every problem listed at once, rather than
being quietly ignored.

## Running it

The base build is six processes: the Concorde service and five chains.
The easy way is the run profile, which starts all six with one command:

```bash
python main.py run concorde_test
```

Each one is still its own OS process; the profile just launches them
together and treats them as one unit. If any one of them exits or
crashes, the launcher stops all the others, and Ctrl-C stops the lot.
Profiles live in `config.toml`:

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

A profile starts every service in the `services` list as well as its
chains. A service that is already running — under another profile, say —
is left alone rather than started twice; a service refuses to run twice
anyway.

Or run each process by hand, one per terminal, as
`python main.py <family> <name>`:

```bash
python main.py service        concorde
python main.py ingest_chain   concorde_A
python main.py ingest_chain   concorde_B
python main.py snapshot_chain snapshot_chain
python main.py output_chain   display
python main.py output_chain   history
```

Started by hand, they can be started in any order and stopped and
restarted individually; the others carry on.

The `display` chain prints the picture every five seconds:

```
[21:40:26Z] display: 1 aircraft
  HEX      CALLSIGN   REG      TYPE  SQWK        LAT         LON     ALT    SPD   HDG     V/S  ROUTE     SOURCE
  400F6A   BAW002     G-BOAC   CONC  2346    52.8328     -1.0000    2000    300   180    1000  LHR->JFK  concorde_A, concorde_B
```

`SOURCE` lists every module that has reported the aircraft. It is not
stored: it is read off the keys of the record's `raw`, which holds each
reporting module's payload under its own name.

Concorde flies a 100nm pass over the observer at 300 knots — twenty minutes
a pass — climbing from 2,000ft at spawn to 12,000ft overhead and back down
again, then starts a new pass on a fresh cardinal bearing. The service
publishes her position about once a second; a restarted service resumes the
pass in progress.

### Service settings

Which services run is the `services` list. Each one's settings go in an
optional `[service.<name>]` block — only needed to change something:

```toml
services = ["concorde"]

[service.concorde]
debug_level = "info"    # default "warn"
```

Every service takes `debug_level`. A service with options of its own
declares them as a `ServiceConfig` subclass (its `config_class`), and its
block is validated against that — an unknown key is an error, as it is
everywhere else. A block for a service that is not in the `services` list
is an error too, rather than being quietly ignored.

### Output chains: live and deletions

Every output chain has exactly one input, set by its `source`:

- `source = "live"` — reads the snapshot every `poll_interval_seconds`,
  runs its transforms, and calls the module's `send()` with the whole
  picture.
- `source = "deletions"` — never reads the live snapshot. It follows the
  deletion notices and calls the module's `on_deleted()` once for each
  aircraft that expires.

A destination that wants both is two chains of the same module type, one
of each — which is exactly what `display` and `history` are for the
console.

### Watching expiry

Concorde is re-observed every few seconds, so she never goes stale while
the service and ingest chains are running. To watch her expire, stop them
while leaving the snapshot and `history` chains running — by hand, or with
a profile that lists only those two chains. After `expiry_minutes`
(default 5) the snapshot process deletes her file and `history` reports it
once:

```
  GONE  400F6A BAW002     last seen 2026-09-12 21:04:19.764102+00:00  source concorde_A, concorde_B
```

Each deletions chain keeps its own cursor into the notices, so every such
chain sees every deletion exactly once, however fast or slow it polls, and
a restarted chain picks up where it left off.

## How a chain is put together

The order is fixed by chain family, not configurable per chain:

```
ingest chain     poll a source  ->  transforms  ->  snapshot.save()
snapshot chain   transforms  ->  merge into the snapshot; separately, expire stale records
output chain     transforms  ->  send() the picture  (live)
                             ->  on_deleted() each expiry  (deletions)
```

Ingest is core-then-transform, because there is nothing to transform until
the source has produced it. Snapshot and output are transform-then-core.

## The merge rule

Two ingest chains write into the same picture with no coordination beyond a
per-aircraft lock, so the rule they both apply is the whole design. It is
three independent rules, not one:

1. **`raw[<module>]`** — the incoming module's own key is written
   unconditionally; every other module's key is left alone. Several
   sources reporting the same aircraft keep their payloads side by side.
2. **`location`, `direction`, `last_seen`** — taken only if the incoming
   `last_seen` is newer than the one held, and then as a complete set. A
   position only makes sense from whichever source saw it most recently,
   and filling its blanks from an older one would place the aircraft
   somewhere it has never been.
3. **Everything else** (`route`, `airframe`, `airline`, `meta.squawk`,
   `meta.icao_hex`) — no date check. Any value the incoming record actually has overwrites the one
   held; a blank incoming value is skipped. A later, fuller report can
   correct an earlier one. Two sources that genuinely disagree would flap
   between values, whoever last wrote real data winning.

`meta.first_seen` keeps the earliest time on offer — a record's own
`first_seen`, or failing that its `last_seen` — so it marks when the
aircraft was first seen for as long as its snapshot record lives.

The whole read-merge-write sequence runs under the lock, not just the write.
Two processes that each read the old record and then each write would both
produce a file that was individually consistent and jointly missing what
the other had learned.

## Layout

```
main.py              entrypoint — one process, one chain or service; or a run profile
config.py            loads and validates config.toml; nothing else reads it
schemas/aircraft.py  the Aircraft dataclasses, exactly as the spec; every hand-off is list[Aircraft]
chains/              the three runners, the run-profile launcher, and what they share
ingest/              BaseIngest + the Concorde ingest module
snapshot/            BaseSnapshot + the file_object backend and its merge
output/              BaseOutput + the console display
transforms/          BaseTransform + nop
services/            BaseService (single-writer process loop) + the Concorde simulator
tests/
data/                gitignored; all runtime state
```

Each module package's `__init__.py` holds a registry mapping the `type`
name used in `config.toml` to the class. Adding a module is: write the
class, add one line to that registry, reference it from config.

## Tests

```bash
./runtests.sh          # or: python -m pytest tests/ -v
```

The snapshot concurrency tests and the launcher tests use real OS processes
rather than threads, deliberately: the snapshot's lock is an `flock` whose
entire job is to work between processes, and a threaded test would pass
with no lock at all; the launcher's job is signals and exit statuses
between processes.

## Licence

See `LICENSE`.
