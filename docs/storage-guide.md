# Storage Backends — Developer Guide

A **storage backend** is the persistence layer that sits between ingestors and the processor. Ingestors write aircraft records to it; the processor reads from it. Backends are interchangeable: switching from `disk_drive` to `sqlite` is a single config change with no code changes elsewhere.

```
ingestor → storage ← processor → modules → display
```

## Responsibilities

A storage backend owns three things:

- **Upsert-if-newer writes** — a record is only written if its `meta.observed_at` is more recent than what is already on disk for that ICAO hex. This makes multi-ingestor writes safe without any coordination between ingestors.
- **Staleness expiry** — records not updated within `STALE_SECONDS` (60 seconds, defined in `storage/__init__.py`) are automatically cleaned up. This is triggered internally on every write, not by the caller.
- **Fresh reads** — `retrieve_aircraft_array()` only returns records that are within the staleness window, so the processor always sees current aircraft regardless of when cleanup last ran.

## The `STALE_SECONDS` constant

```python
# storage/__init__.py
STALE_SECONDS = 60
```

This is the single global threshold for what counts as a live aircraft. It is not configurable — 60 seconds is always correct for a live system. Backends import and enforce it; nothing outside storage ever needs to know about it.

## The interface

```python
class BaseStorage:
    def save_aircraft_array(self, aircraft: list[Aircraft]) -> None: ...
    def list_aircraft_hex_ids(self)                         -> list[str]: ...
    def retrieve_aircraft(self, hex_id: str)                -> dict | None: ...
    def retrieve_aircraft_array(self)                       -> list[dict]: ...
```

The asymmetry between writes (typed `Aircraft`) and reads (raw `dict`) is intentional. Writers come from the ingest pipeline with schema objects in hand; readers consume JSON-shaped data and shouldn't have to reconstruct typed objects.

## Selection

The storage backend is selected once in config and shared by all ingestors and the processor:

```toml
[storage]
backend = "disk_drive"
```

Both ingestors and the processor resolve it the same way:

```python
from storage import get_storage
storage = get_storage(config.storage.method, config.squawk.data_dir)
```

The dispatcher resolves the name to `storage/<name>/` and calls its `get(data_dir)` factory.

## Worked example: `disk_drive`

Each aircraft is stored as an individual JSON file under `<data_dir>/tracked_aircraft/<icao_hex>.json`. The file's `mtime` is the freshness signal — it updates every time a newer observation is written.

### Write (upsert-if-newer)

```python
def save_aircraft_array(self, aircraft: list[Aircraft]) -> None:
    for a in aircraft:
        path = self.aircraft_dir / f"{a.meta.icao_hex}.json"

        if a.meta.observed_at is not None and path.exists():
            try:
                existing    = json.loads(path.read_text())
                existing_oa = existing.get("meta", {}).get("observed_at")
                if existing_oa and a.meta.observed_at.isoformat() <= existing_oa:
                    continue  # on-disk record is same age or newer
            except (OSError, ValueError):
                pass  # unreadable — fall through and overwrite

        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(a, cls=SquawkEncoder, indent=2))
            tmp.replace(path)
        except OSError:
            pass  # silent; next cycle will retry

    self._expire_stale()
```

Three patterns to take away:

- **Upsert-if-newer.** Before writing, read the existing record and compare `meta.observed_at`. Skip the write if the stored observation is at least as recent. A record with no `observed_at` always writes (safe fallback for synthetic sources).
- **Atomic writes via temp-and-replace.** Write to `<file>.tmp`, then `replace()` into place. A reader can never see a partial file.
- **Internal expiry.** `_expire_stale()` is called at the end of every write cycle — it's a private method, never called by ingestors or the processor.

### Read (staleness-filtered)

```python
def retrieve_aircraft_array(self) -> list[dict]:
    result = []
    now = time.time()
    for path in self.aircraft_dir.glob("*.json"):
        if now - path.stat().st_mtime > STALE_SECONDS:
            continue
        try:
            result.append(json.loads(path.read_text()))
        except (OSError, ValueError):
            pass
    return result
```

Stale files are skipped on read without being deleted — the next write cycle's `_expire_stale` will clean them up. This means the processor always receives a fresh view even if no write has happened recently enough to trigger cleanup.

### Corruption recovery

If a file is unreadable (truncated write, disk error), it is deleted. The next ingestor cycle will rewrite valid data.

## Other candidates

Plausible alternatives — sketches only.

- **sqlite** — a single-file SQL database. `save_aircraft_array` becomes `INSERT OR REPLACE WHERE observed_at > existing.observed_at`; expiry becomes `DELETE WHERE updated_at < ?`; staleness filtering on read becomes a `WHERE` clause. Better story for indexed lookups and concurrent readers.
- **memory** — in-process dict, no persistence. For tests and ephemeral debug runs. Trivial to implement; useful for keeping unit tests off disk.
- **redis** — key/value store keyed by ICAO hex. TTL-based expiry replaces `_expire_stale`. No history by design — a good fit for the live-only use case.

All fit the existing interface. None require the interface to change.

## Writing your own — checklist

1. **Subclass `BaseStorage`** and implement all four methods.
2. **Add the factory:** `def get(data_dir: Path) -> YourStorage`.
3. **Implement upsert-if-newer in `save_aircraft_array`.** Compare `meta.observed_at` before writing; only write if the incoming record is strictly newer than what is stored. A record with `observed_at = None` always writes.
4. **Call `_expire_stale()` at the end of `save_aircraft_array`.** Expiry is internal — callers never trigger it directly.
5. **Filter by staleness in `retrieve_aircraft_array` and `retrieve_aircraft`.** Return `None` / skip records older than `STALE_SECONDS`.
6. **Make writes atomic.** Temp-and-replace, transactions, copy-on-write — whatever fits your medium.
7. **Swallow write failures.** Log if you must, but don't raise. The pipeline will retry on the next cycle.
8. **Return raw dicts from reads.** Don't reconstruct `Aircraft` — that's the caller's job.
9. **Return `None` for missing or stale reads.** Not raise.
10. **Clean up corrupt records.** If you read a record that's malformed, delete it. The next cycle will rewrite valid data.

## Testing

- Build the backend pointing at `tmp_path` (pytest) or in-memory equivalent.
- Save an `Aircraft` with a known `observed_at`. Save again with an older `observed_at`. Assert the on-disk record is unchanged.
- Save an `Aircraft` with a known `observed_at`. Save again with a newer `observed_at`. Assert the on-disk record is updated.
- Save an aircraft. Age the file past `STALE_SECONDS`. Save any other aircraft (triggering expiry). Assert the stale file is gone.
- Age a file past `STALE_SECONDS`. Call `retrieve_aircraft_array`. Assert it is absent from the result.
- Corrupt a file by hand. Call `retrieve_aircraft`. Assert it returns `None` and the corrupt record is cleaned up.

## Common pitfalls

- **Non-atomic writes.** The single biggest source of mysterious bugs. If a reader can see a half-written file, you have a race. Always write to a sibling and rename.
- **Missing the upsert check.** Without it, a slow ingestor can overwrite a faster one's fresher data. Always compare `observed_at` before writing.
- **Making expiry public.** Expiry is an internal detail of the backend. If callers can trigger it independently, you have split responsibility. Keep it private, called automatically on write.
- **Not filtering on read.** If `retrieve_aircraft_array` returns stale records, the processor will display aircraft that have left range. Filter by `STALE_SECONDS` in the read path.
- **Raising on read miss.** Callers expect `None` for "not there". Raising forces every caller to wrap in `try/except`.
- **Returning typed objects from reads.** Don't. Callers want dicts.
