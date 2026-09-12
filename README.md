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
| Ingest → snapshot | The ingest process merges into `data/snapshot/` itself, in-process. |
| Snapshot → output | The output process reads `data/snapshot/*.json` on its own poll. |
| Aircraft expired | The snapshot process leaves a notice in `data/snapshot/_deletions/`. |
| Concorde's position | A locked state file in `data/services/concorde/`. |

So "the snapshot" is a folder plus a merge rule that every writer applies
identically, and "the Concorde service" is a state file plus the pure
functions that derive a position from it. Neither is a running process, and
a chain started on its own works perfectly well with nothing else running —
it just has nobody to talk to.

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
validation is strict — an unknown key, module type or transform stops
Squawk at start-up with every problem listed at once, rather than being
quietly ignored.

## Running it

Every process is `python main.py <chain_family> <name>`, where `name` is
the config block's key. The base build is five of them, one per terminal:

```bash
python main.py ingest_chain   concorde_A
python main.py ingest_chain   concorde_B
python main.py snapshot_chain snapshot_chain
python main.py output_chain   display
python main.py output_chain   history
```

There is no sixth process for the Concorde service — see above. Start them
in any order, stop any of them with Ctrl-C, and start it again whenever;
the others carry on.

Both output chains print a line per aircraft every five seconds:

```
[17:53:44Z] display: 1 aircraft
  HEX      CALLSIGN   REG      TYPE       LAT       LON     ALT   SPD   TRK    V/S   DIST  ROUTE
  400F6A   BAW002     G-BOAC   CONC   52.7781   -1.0000    2657   300   180   1000   46.7  LHR->JFK
```

Concorde flies a 100nm pass over the observer at 300 knots — twenty minutes
a pass — climbing from 2,000ft at spawn to 12,000ft overhead and back down
again, then starts a new pass on a fresh cardinal bearing.

### Watching expiry

Concorde is re-observed every few seconds, so she never goes stale while
the ingest chains are running. To watch an aircraft expire, stop them:

```bash
# in the concorde_A and concorde_B terminals
Ctrl-C
```

After `expiry_minutes` (default 5) the snapshot process deletes her file
and both output chains report it once each:

```
  GONE  400F6A BAW002     last seen 2026-09-12 17:54:01.939640+00:00
```

Each output chain keeps its own cursor into the deletion notices, so every
chain sees every deletion exactly once, however fast or slow it polls, and
a restarted chain picks up where it left off.

## How a chain is put together

All three families are the same three steps in a different order:

```
ingest chain     poll a source  ->  transforms  ->  snapshot.save()
output chain     snapshot.read_all()  ->  transforms  ->  output.send()
snapshot chain   scan for stale records  ->  delete  ->  notify
```

The snapshot has a transform list of its own, applied to every incoming
batch before it is merged.

## The merge rule

Two ingest chains write into the same picture with no coordination beyond a
per-aircraft lock, so the rule they both apply is the whole design:

- **Not newer than what is held?** Discarded. The loser of a race is
  carrying a worse answer, not a newer one.
- **`location`, `direction` and `raw`** always overwrite. A position is only
  meaningful as a complete set; filling its blanks from an older record
  would place the aircraft somewhere it has never been.
- **Everything else** fills in only where the snapshot's value is still
  unknown. That is what lets one chain observe an aircraft and another
  enrich it without either clobbering the other's work.
- **`last_seen`** always advances, being the thing all of the above is
  measured on.

The whole read-merge-write sequence runs under the lock, not just the write.
Two processes that each read the old record and then each write would both
produce a file that was individually consistent and jointly missing what
the other had learned.

## Layout

```
main.py              entrypoint — one process, one chain
config.py            loads and validates config.toml; nothing else reads it
schemas/aircraft.py  the Aircraft dataclasses; every hand-off is list[Aircraft]
chains/              the three runners, and what they share
ingest/              BaseIngest + the Concorde ingest module
snapshot/            BaseSnapshot + the file_object backend and its merge
output/              BaseOutput + the console display
transforms/          BaseTransform + nop
services/            BaseService + the Concorde simulator
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

The concurrency and locking tests use real OS processes rather than
threads, deliberately: both locks are `flock`s whose entire job is to work
between processes, and a threaded test would pass with no lock at all.

## Licence

See `LICENSE`.
