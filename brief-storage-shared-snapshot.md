# Brief: share one storage instance and cache the deserialised snapshot

**Scope:** `storage/__init__.py`, `storage/disk_drive.py`, `processor/processor.py`, and
`tests/`. Do not touch `config.py`, `config.toml`, `modules/`, `display/`, or `ingestor/`.

---

## Problem

`get_storage()` constructs a fresh backend on every call. There are eight enabled
processor chains plus the ingestors, so every one of them holds its own
`DiskDriveStorage`.

Worse, each processor thread then does its own full read and deserialisation every
cycle. With ~200 aircraft in range that is, per chain per cycle: a directory glob, ~200
`stat` calls, ~200 file reads, ~200 `json.loads`, and ~200 `aircraft_from_dict`
conversions. Multiplied by eight chains, all producing byte-identical results from the
same files.

This is the last of the "eight private copies of everything" problems. The design intent
was that a cycle reads storage once and chains operate on shared `Aircraft` references.

Chains keep their own threads and their own poll schedules. This change only stops them
duplicating identical work.

---

## Change 1 — shared backend instances in the factory

In `storage/__init__.py`, add an instance pool keyed by `(method, data_dir)`:

```python
_INSTANCES: dict[tuple[str, str], BaseStorage] = {}
_INSTANCES_LOCK = threading.Lock()


def get_storage(method: str, data_dir: Path) -> BaseStorage:
    key = (method, str(data_dir))
    with _INSTANCES_LOCK:
        if key not in _INSTANCES:
            try:
                module = importlib.import_module(f"storage.{method}")
            except ModuleNotFoundError:
                raise ValueError(f"Unknown storage method: {method!r}")
            _INSTANCES[key] = module.get(data_dir)
        return _INSTANCES[key]
```

Unlike the `adsbdb` change, keying **is** warranted here — `data_dir` genuinely varies
(tests pass `tmp_path`). Keep the `ValueError` for an unknown method raising on every
call, not just the first: the import must stay inside the lock and before the cache
write, as above. `tests/test_processor.py` asserts this.

---

## Change 2 — cached deserialised snapshot on `BaseStorage`

Add a concrete method and a TTL cache to the `BaseStorage` ABC, so every backend
inherits it and a future backend gets it for free:

```python
SNAPSHOT_TTL_SECONDS = 1.0


class BaseStorage(ABC):

    def __init__(self) -> None:
        self._snapshot_lock = threading.Lock()
        self._snapshot: list[Aircraft] = []
        self._snapshot_at = 0.0

    def retrieve_aircraft_objects(self) -> list[Aircraft]:
        """Deserialised snapshot, shared across callers within the TTL window."""
        now = time.monotonic()
        with self._snapshot_lock:
            if now - self._snapshot_at > SNAPSHOT_TTL_SECONDS:
                self._snapshot = [
                    aircraft_from_dict(d) for d in self.retrieve_aircraft_array()
                ]
                self._snapshot_at = now
            return list(self._snapshot)

    # ... existing abstract methods unchanged
```

Import `aircraft_from_dict` alongside the existing `Aircraft` import.

Notes on the specifics, all deliberate:

- **Holding the lock across the disk read is intended.** If several chains arrive at an
  expired cache together, the first refreshes and the rest wait and receive the fresh
  result, rather than all duplicating the read.
- **`list(self._snapshot)` returns a shallow copy.** Chains get their own list object so
  no chain can disturb another's iteration, but the `Aircraft` objects inside are shared.
  That sharing is the point.
- **TTL only — no write invalidation.** `save_aircraft_array()` does not need to clear
  the cache. A 1-second stale window is immaterial when records are already up to 5
  seconds old at ingest and `STALE_SECONDS` is 60. Adding invalidation would mean
  coordinating ingestor writes with processor reads for no observable benefit.
- **`retrieve_aircraft_array()` stays as it is** and remains uncached. It is the raw
  read that `retrieve_aircraft_objects()` is built on, and existing tests call it
  directly.

`DiskDriveStorage.__init__` must now call `super().__init__()` as its first statement.
Existing tests construct `DiskDriveStorage(tmp_path)` directly, so this must not break.

---

## Change 3 — processor uses the cached snapshot

In `processor/processor.py`, replace:

```python
aircraft = [aircraft_from_dict(d) for d in storage.retrieve_aircraft_array()]
```

with:

```python
aircraft = storage.retrieve_aircraft_objects()
```

Remove the now-unused `aircraft_from_dict` import from that file.

---

## Shared `Aircraft` objects — expected, not a bug

After this change, chains running within the same TTL window hold references to the same
`Aircraft` objects. Enrichment performed by one chain becomes visible to another. That is
the original design intent — work done once, seen by all.

It is safe here because enrichment modules only fill fields that are `None`, with the
same value drawn from the same cache. The worst concurrent case is two threads writing an
identical string to the same attribute.

Do not add locking around `Aircraft` field access, and do not deep-copy the snapshot per
chain. Either would undo the change.

---

## Explicitly out of scope

- Any new storage backend, SQLite or otherwise.
- Moving `tar1090_db` to the ingestors (that is a separate brief, and it changes
  `config.toml`).
- Any change to `STALE_SECONDS`, the on-disk format, or the upsert-if-newer logic.
- Any change to how ingestors obtain storage — they call `get_storage()` already and
  pick up the shared instance automatically.

---

## Tests

Add to `tests/test_snapshot.py` (or a new `tests/test_storage_shared.py`, your call):

1. **Shared instance** — two `get_storage("disk_drive", tmp_path)` calls return the same
   object; a call with a different `data_dir` returns a different one. Reset
   `storage._INSTANCES` in a fixture so tests are order-independent.

2. **Unknown method still raises** — `get_storage("oracle_db", tmp_path)` raises
   `ValueError` on both the first and a second call.

3. **Snapshot is cached** — save two aircraft, call `retrieve_aircraft_objects()`, then
   save a third and call again immediately. The second call returns two aircraft, because
   the TTL has not elapsed. Then monkeypatch `time.monotonic` forward past the TTL (or set
   `_snapshot_at = 0.0`) and assert the third now appears.

4. **Objects are shared, list is not** — two `retrieve_aircraft_objects()` calls within
   the TTL return different list objects (`is not`) whose elements are the same objects
   (`is`). Mutating the returned list must not affect a subsequent call.

5. **Concurrent refresh reads once** — monkeypatch `retrieve_aircraft_array` with a
   counting wrapper, start ~10 threads calling `retrieve_aircraft_objects()` at once from
   a cold cache, assert the underlying read ran exactly once.

Existing tests in `tests/test_snapshot.py` construct `DiskDriveStorage(tmp_path)`
directly and call `retrieve_aircraft_array()`; all must pass unchanged.

---

## Verification

- `./runtests.sh` passes.
- Start Squawk with the live eight-chain `config.toml`. All chains start, all eight panels
  populate, and the numbers match what they showed before.
- On the Pi, confirm the drop in read load — `iostat` or simply that CPU on the Squawk
  process falls noticeably compared with the current build.
