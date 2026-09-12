# Squawk v2 — Base Implementation Brief

## Goal

Build the base program end-to-end: two Concorde ingest-chain processes feeding
one snapshot, which two output-chain processes poll and console.log. All
transforms are `nop`. This proves every module type and chain type in the
spec actually works together, so future feature work only ever means adding
a module — no core changes.

## Implementation language

Python (sole implementation language — no other languages or runtimes).

## Scope

**In scope:** schema, config loading, module interfaces (Ingest, Transform,
Snapshot, Output, Service), the concorde service, `ingest_concorde` ingest
module, `file_object` snapshot module, `output_console` output module, `nop`
transform, and the chain runners that wire them together as independent OS
processes.

**Out of scope:** real ADS-B ingestors, real enrichment (VRS/tar1090_db/adsbdb),
real displays (HTTP wall, e-paper, Pushover), priority overrides (deliberately
dropped — see spec), snapshot-query API (parked for v2.1).

## Decision: everything is filesystem, nothing is networked

This is open-source software: users `git clone`, edit `config.toml`, and run
it. They should never have to stand up an extra service, open a port, or
worry about start-up order. So all inter-process communication for this
build is via files under `data_dir` — there is no localhost HTTP anywhere in
this brief:

- **Ingest → Snapshot.** Each ingest process performs the merge/upsert
  itself, writing directly into the shared `<data_dir>/snapshot/` folder
  (atomic write-then-rename). No snapshot process needs to be running first
  — "the snapshot" is just that folder plus the merge rule every writer
  applies consistently.
- **Snapshot → Output.** Each output process reads
  `<data_dir>/snapshot/*.json` directly on its own poll interval. No push,
  no endpoint.
- **Concorde service.** Backed by a single locked state file under
  `<data_dir>/services/concorde/`, not a running server. See §5 — the
  position at any moment is a pure function of that state, so any process
  that reads it computes the identical answer.

This also means "the snapshot" and "a service" are not necessarily separate
running processes at all in this build — they're conventions plus a shared
file. A future *real* external service (FlightAware, weather) will need an
actual process making outbound calls and refreshing a local cache — but even
then, the modules that consume it only ever read that cache file, never call
the service process directly. Keep that boundary in mind so the pattern
generalises later without a rewrite.

## Project layout (proposed)

```
squawk/
    main.py                  # entrypoint: `python main.py <chain_family> <name>`
    config.py                 # loads + validates config.toml into typed config objects
    schemas/
        aircraft.py            # Aircraft and sub-object dataclasses
    transforms/
        base.py                # BaseTransform interface
        nop.py
    services/
        base.py                # BaseService — shared locked-state-file helper
        concorde/
            service.py           # Concorde flight simulator — pure functions over state file
    ingest/
        base.py                # BaseIngest interface
        concorde/
            ingestor.py           # reads concorde state, builds Aircraft, runs transform, merges into snapshot
    snapshot/
        base.py                # BaseSnapshot interface
        file_object.py           # SAVE()/upsert/expiry logic against <data_dir>/snapshot/
    output/
        base.py                # BaseOutput interface
        console.py               # prints aircraft array
    chains/
        ingest_chain.py          # runner used by main.py for any ingest_chain.* config block
        snapshot_chain.py        # runner for the single snapshot_chain block (expiry loop only — see §7)
        output_chain.py          # runner used by main.py for any output_chain.* config block
    data/                     # gitignored; runtime state
    config.toml.example
    config.toml               # gitignored
```

## 1. Schema — `schemas/aircraft.py`

Define as dataclasses (or pydantic models — implementer's choice; state which
was picked in the completion report) matching the spec exactly:

- `Airport`, `Airline`, `Airframe`, `Meta`, `Location`, `Direction`, `Route`,
  `Aircraft`, with `raw: dict[str, Any]` on `Aircraft`.
- Every field optional. Ingest sources populate incrementally, and the spec's
  "only update if blank" rule requires distinguishing "not yet known" from
  "zero/empty string" — use `None` as the "not yet known" sentinel throughout.
- All chain hand-offs use `list[Aircraft]`, even for single-aircraft sources —
  per the spec's "data should be passed as an array for compatibility."
- Provide `Aircraft.to_json()` / `Aircraft.from_json()` (or equivalent), since
  aircraft are written to and read from disk as JSON.

## 2. Config — `config.py`

Parse `config.toml` into a typed structure covering:

```toml
data_dir = "data"
observer_latitude = 52.00
observer_longitude = -1.00
services = ["concorde"]

[snapshot_chain]
type = "file_object"
transform = ["nop"]
expiry_minutes = 5

[ingest_chain.concorde_A]
type = "ingest_concorde"
transform = ["nop"]

[ingest_chain.concorde_B]
type = "ingest_concorde"
transform = ["nop"]

[output_chain.display]
type = "output_console"
transform = ["nop"]
poll_interval_seconds = 5

[output_chain.history]
type = "output_console"
transform = ["nop"]
poll_interval_seconds = 5
```

Requirements:

- Strict validation — unknown top-level keys, or unknown chain `type` values,
  fail loudly at startup, not silently ignored. (No silent defaults — existing
  project value, carried over.)
- Exactly one `[snapshot_chain]` block — more than one is a config error.
- `data_dir` defaults to `"data"` if absent.
- No `port` settings anywhere in this build — nothing here listens on the
  network.

## 3. Module interfaces

One shared interface per module type, all taking/returning `list[Aircraft]`
where they touch aircraft data:

- `BaseTransform.process(aircraft: list[Aircraft]) -> list[Aircraft]`
- `BaseIngest.poll() -> list[Aircraft]` (called once per ingest chain loop iteration)
- `BaseSnapshot.save(aircraft: list[Aircraft]) -> None` and `BaseSnapshot.read_all() -> list[Aircraft]`
- `BaseOutput.send(aircraft: list[Aircraft]) -> None`
- `BaseService` — a small helper for reading/writing a service's own state
  file under `<data_dir>/services/<name>/` with a lock (e.g. `fcntl.flock`)
  guarding read-modify-write. No networking.

Every concrete module (`nop`, `ingest_concorde`, `file_object`,
`output_console`, `concorde` service) subclasses the relevant base.

## 4. `transforms/nop.py`

Returns the input list unchanged. Exists purely to prove the transform
interface is wired correctly everywhere it's used (ingest, snapshot, and
output chains all reference `transform = ["nop"]`).

## 5. `services/concorde/service.py`

Simulates Concorde flying a circuit around the observer, climbing and
descending. Deliberately **not** a running process — it's a set of pure
functions over a shared, locked state file, so both ingest instances derive
the identical current position independently:

- `get_position() -> dict` — reads `<data_dir>/services/concorde/state.json`
  under a lock. If no pass is in progress, or the current pass has completed,
  starts a new one (new random cardinal bearing, new spawn point 50nm out)
  and persists it before returning. Otherwise derives the current
  lat/long/altitude/speed/heading from elapsed time since the pass's
  `start_time` — no position is ever stored, only the pass parameters, so
  restarts resume smoothly rather than resetting.
- The lock matters here specifically: two ingest processes calling
  `get_position()` moments apart must not both decide "no pass in progress"
  and independently start two different flights. Whichever call takes the
  lock first either finds a valid in-progress pass or starts one; the second
  call takes the lock after and sees the result of the first.
- Climb/descend profile: implementer's choice (e.g. climb for the first half
  of the pass, descend for the second) — note the chosen profile in the
  completion report.

## 6. `ingest/concorde/ingestor.py`

Runs as its own process per config instance
(`python main.py ingest_chain concorde_A`):

- Loop: sleep a random 5–20s, call the concorde service's `get_position()`,
  build an `Aircraft` object (fixed identity fields — hex, registration,
  callsign, etc.; use sensible realistic values, implementer's choice, this
  is a fresh build not a port), run it through the chain's configured
  transform list, then call the snapshot module's `save()` directly with the
  resulting `list[Aircraft]`.
- Each aircraft carries its own `last_seen` timestamp (set at poll time) —
  this is what the snapshot's upsert logic compares.

## 7. `snapshot/file_object.py`

Two entry points, used by different processes — there is no separate
"snapshot process" for writes, only for expiry (see below):

- **`save(aircraft: list[Aircraft])`** — called directly, in-process, by
  whichever ingest chain produced the data. For each aircraft:
  - If no file exists for its `icao_hex`: run the snapshot chain's own
    transform list on it, then write `<data_dir>/snapshot/<ICAO_HEX>.json`.
  - If a file exists: compare `last_seen`. If the incoming record isn't
    newer, discard it. If it is newer, `location`/`direction`/`raw` always
    overwrite; every other field only overwrites if the existing value is
    `None` and the incoming value isn't.
  - Note: the transform list is applied once per incoming batch, before the
    merge decision — not re-run per field. Confirm with Mortimer if this
    reading seems wrong once you're implementing it.
  - Use a per-file lock (or lock-then-read-then-write) around the whole
    read-merge-write sequence — two ingest chains can call `save()` for the
    same hex within moments of each other, and the merge logic above only
    gives the right answer if that sequence is atomic, not just the final
    write.
- **`read_all() -> list[Aircraft]`** — called directly, in-process, by
  whichever output chain wants the current picture. Reads every file in the
  snapshot folder.
- **Expiry** — runs as its own process
  (`python main.py snapshot_chain snapshot_chain`): a loop that wakes every
  ~30s, scans the folder, and deletes any aircraft whose `last_seen` exceeds
  `expiry_minutes`. Each deletion is handed to every configured output
  chain — for this build, that means writing the deleted aircraft to a
  `<data_dir>/snapshot/_deletions/` folder that output chains also check on
  each poll (or note in the completion report if deletion notification is
  stubbed for now instead — implementer's call, but state which was done).

Use atomic writes (write to a temp file, then rename) for every file write.

## 8. `output/console.py`

`send()` prints the aircraft list — one line per aircraft is fine,
implementer's choice of exact format, keep it readable.

## 9. `chains/output_chain.py`

Runs as its own process (`python main.py output_chain display`):

- Loop: sleep `poll_interval_seconds`, call the snapshot module's
  `read_all()` directly, run the result through the chain's transform list,
  call the output module's `send()`.

## 10. `main.py` — process entrypoint

`python main.py <chain_family> <name>` where `chain_family` is one of
`ingest_chain`, `snapshot_chain`, `output_chain`, and `name` is the config
block's key. Looks up the matching config block, instantiates the right
module + transform list, and runs that runner's loop. Five processes for
this initial setup (no separate concorde-service process — it's a shared
file the two ingest processes read):

```
python main.py ingest_chain concorde_A
python main.py ingest_chain concorde_B
python main.py snapshot_chain snapshot_chain
python main.py output_chain display
python main.py output_chain history
```

## 11. Logging

Each module accepts a `debug_level` in its config block
(`error` | `warn` | `info` | `debug`, default `warn`). Use Python's stdlib
`logging` module, one logger per module instance, named after its config
key. `debug` level logs every service call and merge/skip decision a module
makes. Nothing fancier is required.

## 12. Module self-optimisation

Not meaningfully exercised by `nop` or the Concorde ingestor (nothing to
cache yet), but the interfaces above should be built so a future module
*can* hold its own state between calls without any chain/runner changes —
i.e. don't design `BaseTransform`/`BaseIngest` as stateless-only; a plain
instance attribute is enough.

## Testing

- Unit tests for the snapshot merge logic: new record, newer update,
  older-update-discarded, blank-field-fill, always-overwrite fields. This is
  the part most worth locking down with tests before it's trusted.
- Unit tests for the concorde service's locking: two near-simultaneous
  `get_position()` calls with no existing state must result in exactly one
  new pass, not two.
- Unit tests for config validation: missing snapshot_chain, duplicate
  snapshot_chain, unknown `type`.
- Manual/live verification: run all five processes, watch `data/snapshot/`
  populate and update, watch both console output chains print, confirm
  expiry removes stale files after `expiry_minutes`.

## Deliverable

All of the above, plus a short `README.md` section on how to run the five
processes locally, plus `config.toml.example` reflecting the config in this
brief.


=======================================
Done. All 63 tests pass, and I ran the five processes live end to end.

What's there
Base build complete at the repo root (the brief's squawk/ folder is the repo root, matching V1's layout — main.py and config.py at top level).


main.py  config.py  schemas/  chains/  ingest/  snapshot/  output/  transforms/  services/  tests/
~3,350 lines including tests. Every module type subclasses its base; each package's __init__.py is a registry mapping the config.toml type name to a class, so adding a feature is: write the class, add one registry line, reference it from config. No core changes.

Decisions the brief asked me to state
Schema: plain dataclasses, not pydantic — no dependency, and Optional[...] = None throughout gives the "not yet known" sentinel the merge rule needs. to_json/from_json are written against the dataclass definitions generically, so adding a field needs no serialiser edit and old snapshot files still load.

Concorde profile: linear climb from 2,000 ft at spawn to 12,000 ft overhead, mirrored descent back to 2,000 ft at despawn. Constant 300 kt, so vertical rate is a clean ±1,000 fpm either side of the midpoint — easy to eyeball on the console. 100 nm pass, twenty minutes.

Deletion notification: implemented, not stubbed. _deletions/ holds timestamp-named notices; each output chain keeps its own cursor at data/output/<name>/deletions_cursor.txt, so every chain sees every deletion exactly once regardless of poll rate, and a restart resumes rather than replaying. BaseOutput.on_deleted() is a no-op by default; console overrides it to print GONE. The snapshot chain prunes notices after deletion_retention_minutes (default 60).

Two things to confirm with you
§7's transform question — I took the clarifying note as authoritative: the snapshot chain's transform list runs once over the incoming batch at the top of save(), before any merge decision, not per-field and not re-run on the merged result. Say if you meant the first reading.

meta.ingest_source is provenance for the track, not the write. It's a fill-if-blank field under your rule, so when concorde_B merges into a record concorde_A created, the field still says concorde_A. I kept the specified behaviour and documented it, but if you want "who last touched this", it needs to join last_seen as an always-overwrite exception.

Also worth flagging: last_seen had to become an always-overwrite exception beyond the three you listed. It's never blank on a stored record, so fill-if-blank would freeze it at the first observation and expire an aircraft mid-flight.

I couldn't find a v2 spec document on the machine (only V1's briefs and docs), so the schema shape is derived from the sub-object names in your brief — Route holds origin/destination as Airport and airline as Airline. Check schemas/aircraft.py against the spec you had in mind.

Found and fixed while building
Console output block-buffers when piped — a chain redirected to a file appeared to produce nothing for minutes. Now flushed per poll.
A chain that failed validation also reported "no ingest chains configured", burying the real error. Now checked against the raw tables.
Live verification
Five processes, real run: snapshot populated and updated, both output chains printed, merges logged from both ingest chains, and after stopping the ingest chains the record expired and each output chain printed GONE exactly once.

The locking tests use real OS processes, not threads. I checked they're load-bearing by disabling the flock — all three concurrency tests fail without it.

I have not committed — you didn't ask, and note.txt suggests you drive the base-code → main squash yourself. Everything is untracked and ready; config.toml and data/ are correctly gitignored. Say the word and I'll commit to base-code.