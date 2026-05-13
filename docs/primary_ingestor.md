# Ingestors

## What an ingestor does

An ingestor is a self-contained module that connects to one data source, collects data on a continuous loop, and writes it directly to storage in the standard schema. That is its only job.

The main process launches each enabled ingestor in its own thread at startup and does not interact with them afterwards. Ingestors don't know about the processor, the display, or each other — they just keep storage current. The processor reads from storage whenever it needs data; it doesn't care which ingestor put it there.

```
ingestor → storage ← processor → plugins → display
```

## Primary ingestors — flight data

Primary ingestors provide the core flight data Squawk is built around. At least one is active at all times.

## Secondary ingestors — context data

Secondary ingestors provide supporting data that enriches or contextualises the primary flight feed. They are optional and independent — the pipeline runs perfectly well without them.

---

# Primary ingestor specification

This is the contract any primary ingestor must satisfy. Two reference implementations already exist (`personal_adsb`, `concorde`), and the same pattern extends to any other flight-data source — for example, a FlightAware AeroAPI collector, an ADSBexchange feed reader, or an OpenSky Network puller.

## Module layout

Each ingestor lives in its own package under `ingestor/`:

```
ingestor/
└── <source_name>/
    ├── __init__.py
    ├── ingestor.py      # main loop, fetch, storage write
    └── converter.py     # raw record → Aircraft (if the source needs it)
```

A `converter.py` is recommended when the source emits per-aircraft records in a foreign schema. Synthetic sources (e.g. `concorde`) that build records directly can skip it.

## Required interface

The `ingestor.py` module must expose:

```python
SOURCE_NAME: str        # human-readable source identifier
def run() -> None       # blocking main loop; runs until interrupted
```

`run()` is invoked in a dedicated thread at startup. It must:

1. Read its own config block from `config.ingestors[<key>]`.
2. Return immediately if `enabled` is false.
3. Resolve the storage backend once at startup via `get_storage(config.storage.method, config.squawk.data_dir)`.
4. Loop until interrupted, converting source data into `Aircraft` objects and calling `storage.save_aircraft_array()` each cycle.
5. Tolerate `KeyboardInterrupt` cleanly when run as `__main__`.

## Configuration

Each ingestor owns one TOML block under `[ingestors.<key>]`. Required keys:

```toml
[ingestors.<key>]
enabled = true
poll_interval_seconds = 5
```

Source-specific keys (API endpoints, credentials, bounding boxes, receiver lists, timeouts) sit alongside. Credentials and personally identifying data must be redacted from `config.toml.example`.

## Polling loop

Every cycle follows the same shape:

1. Record `cycle_start = time.time()`.
2. Fetch from the source.
3. Convert raw records into `Aircraft` objects (one per unique ICAO hex).
4. Call `storage.save_aircraft_array(aircraft_list)`.
5. Sleep for `max(0, poll_interval_seconds - elapsed)`.

The cycle compensates for fetch duration so polling cadence stays stable under variable network conditions. A slow fetch never causes a tight loop; a fast one never overshoots the configured interval.

## Error handling

Network errors, timeouts, and malformed payloads are swallowed — a single failed fetch must not kill the loop. Failures surface through `ReceiverStatus` tracked in memory, not via exceptions.

Records with no usable ICAO hex are dropped silently. The hex is the minimum viable identity; nothing downstream works without it.

## `Aircraft` assembly

Whether built by a converter or directly, every `Aircraft` populates the full schema:

| Section     | Field | Contents |
|-------------|-------|----------|
| `meta`      | `icao_hex` | 24-bit ICAO address, uppercased |
| `meta`      | `ingestor` | Source identifier string, e.g. `"personal_adsb"` |
| `meta`      | `observed_at` | UTC datetime of the last message from this aircraft. Computed as `snapshot.now - seen_seconds`. This is the storage merge key — the storage backend will only write this record if `observed_at` is more recent than whatever is already on disk for this hex. |
| `meta`      | `reception_type` | How the data was obtained (`adsb_icao`, `mlat`, `mode_s`, etc.) |
| `location`  | | Latitude, longitude, altitude, distance from receiver, bearing, seen_seconds |
| `direction` | | Ground speed, track, vertical rate |
| `route`     | | Callsign, squawk code (ATC-assigned per airspace entry), origin, destination, flight number |
| `airframe`  | | Registration, aircraft type, operator |
| `raw`       | `payload` | Complete unmodified source record |

Missing fields are set to `UNKNOWN` (`None`), signalling downstream plugins that enrichment may be needed. The converter never invents data; it never partially maps; it never silently coerces types beyond the documented transforms (e.g. `"ground"` → `0` for altitude).

## `observed_at` — the merge key

`observed_at` is what makes multi-ingestor writes safe. When two ingestors both observe the same aircraft, storage compares `observed_at` and keeps whichever record is more recent, regardless of write order. The ingestor is responsible for computing it accurately:

```python
observed_at = datetime.fromtimestamp(snapshot_now - seen_seconds, tz=timezone.utc)
```

For sources that don't provide a `seen_seconds` equivalent, use `datetime.now(timezone.utc)` at fetch time. A record with `observed_at = None` always writes (safe fallback for synthetic sources).

## Raw passthrough

The `raw` layer is a complete safety net, not a curated selection. The full source payload is stored verbatim in `AircraftRaw.payload` — every field, including the ones the converter has already mapped into the schema. This lets future plugins consume source-specific fields (integrity values, RSSI, wind data, nav modes) without requiring a converter change.

## Multi-receiver sources

When a single logical source aggregates multiple physical receivers (`personal_adsb` is the canonical example), the ingestor is responsible for merging duplicate records before writing to storage. Merge policy: for each unique ICAO hex, keep the record with the most recent `observed_at`. Since `observed_at` is computed from an absolute snapshot timestamp, this correctly handles receivers polled at different times.

The storage layer also applies an upsert-if-newer check on write, providing a second layer of protection when two ingestors cover overlapping airspace.

## Quirks documentation

Every primary ingestor documents the rough edges of its source in its module docstring: missing fields, polymorphic types, unit conventions, staleness, quota behaviour. A field the source never provides should be called out so plugin authors don't waste time wondering why it's always `UNKNOWN`.
