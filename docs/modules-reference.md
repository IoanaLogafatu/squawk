# Squawk — Modules Reference

This is a reference catalogue of every module (filter, enrichment, and display) shipping in this release: what it does, how it's configured, and where it sits in the pipeline.

```
ingestor → storage ← processors (independent chains) → displays
```

**Ingestors** run independently in background threads and write to storage. **Processors** define independent chains running in their own background threads (`[processors.<name>]` in `config.toml`). Each chain runs an ordered sequence of **modules** (filters and enrichments) and hands the resulting aircraft to its configured **display** (`display = "<name>"`).

For the mechanics of writing a new one, see the **Modules Developer Guide** and **Display Modules Developer Guide**. This document is the "what's available" catalogue, not the "how to build one" guide.

## Quick reference

| Name | Category | Config section | Purpose |
|---|---|---|---|
| `personal_adsb` | Ingestor | `[ingestors.personal_adsb]` | Polls your own receiver(s), merges by recency |
| `concorde` | Ingestor | `[ingestors.concorde]` | Synthetic test aircraft, no hardware needed |
| `closest_filter` | Module — Filter | `[modules.closest_filter]` | Reduces to the single nearest aircraft |
| `altitude_filter` | Module — Filter | `[modules.altitude_filter]` | Keeps aircraft within an altitude band |
| `ground_distance_filter` | Module — Filter | `[modules.ground_distance_filter]` | Keeps aircraft within a ground distance range (miles, km, nm) |
| `vertical_rate_filter` | Module — Filter | `[modules.vertical_rate_filter]` | Keeps aircraft climbing, descending, or level |
| `registration_filter` | Module — Filter | `[modules.registration_filter]` | Keeps only a configured tail-number watchlist |
| `tar1090_db` | Module — Enrichment | *(none)* | Fills registration/type from the tar1090 CSV |
| `adsbdb` | Module — Enrichment | `log_unresolved` | Fills manufacturer, owner, and route from adsbdb.com |
| `pass_through` | Module — Utility | *(none)* | No-op placeholder chain slot |
| `console` | Display | `[display.console]` | Prints the nearest aircraft to stdout |
| `http` | Display | `[display.http]` | Live-updating web page (SSE) |
| `epaper` | Display | `[display.epaper]` | Renders to a Waveshare e-paper panel + PNG preview |
| `pushover` | Display | `[display.pushover]` | Push notification for a fully-enriched flight |

---

## Ingestors

Ingestors are configured under `[ingestors.<name>]` and started as independent threads at launch. Each owns its own polling loop and writes directly to storage; the processor never talks to them.

### `personal_adsb`

Polls one or more of your own readsb/tar1090 receivers (`aircraft.json`), merges them into a single view, and converts each record into the Squawk schema.

- **Multi-receiver merge:** for each ICAO hex, keeps whichever receiver's record has the most recent `observed_at` (`snapshot.now - seen`). This is what lets `adsbwest` and `adsbeast` cover overlapping sky without one clobbering the other.
- **Per-receiver health tracking:** a dead receiver is recorded as unhealthy and skipped for that cycle — it doesn't take down the others.
- **Compensated polling:** sleeps `poll_interval_seconds - elapsed`, so a slow fetch never causes a tight loop.

```toml
[ingestors.personal_adsb]
enabled               = true
poll_interval_seconds = 5
timeout_seconds       = 3
receivers = [
    { name = "adsbwest", url = "http://adsbwest.local/tar1090/data/aircraft.json" },
    { name = "adsbeast", url = "http://adsbeast.local/tar1090/data/aircraft.json" },
]
```

### `concorde`

A synthetic fixture — no hardware required. Simulates registration `G-BOAC` flying a straight 100nm pass over your configured observer location at 300kn / 5,000ft, spawning on a random cardinal bearing each time a pass completes. State (current pass) persists to disk so a restart resumes the flight in progress rather than respawning it. Useful for exercising the full pipeline — modules, storage, displays — without waiting for real traffic.

```toml
[ingestors.concorde]
enabled = true
```

---

## Modules

Modules are the ordered chain named in `processors.<name>.modules`; each name maps to a `[modules.<name>]` config table, empty if the module takes no options — the block itself is required, or config loading fails at startup. Every module shares one interface — `process(aircraft) -> aircraft` — so the distinction below is convention, not enforcement.

```toml
[processors.screen]
enabled               = true
poll_interval_seconds = 5
modules               = ["tar1090_db", "registration_filter", "closest_filter", "adsbdb"]
display               = "epaper"
```

### Filters

Filters reduce or reorder the list. Output length is always ≤ input length.

#### `closest_filter`

Reduces the list to the single aircraft nearest the receiver (`location.distance_nm`, ascending). Aircraft with no known distance are excluded as candidates rather than crashing the comparison. Returns `[]` if nothing qualifies.

No configuration options.

#### `altitude_filter`

Keeps only aircraft within an altitude band.

```toml
[modules.altitude_filter]
above           = 10000        # minimum altitude, feet (optional)
below           = 30000        # maximum altitude, feet (optional)
altitude_source = "alt_baro"   # "alt_baro" or "alt_geom"
fallback        = true         # use the other source if the chosen one is missing
```

Reads the source's raw `alt_baro`/`alt_geom` payload fields directly (falling back to `location.altitude_feet` for baro) so it works even before `location.altitude_feet` has been populated. Raises at startup if `above > below`.

#### `ground_distance_filter`

Keeps only aircraft within a ground distance (2D great-circle distance ignoring altitude) range from the observer.

```toml
[modules.ground_distance_filter]
max_distance = 25           # maximum distance (optional)
min_distance = 0            # minimum distance (optional)
unit         = "miles"      # "miles", "km", or "nm" (default "nm")
```

Supports distance units: `"miles"` (or `"mi"`), `"km"` (or `"kilometers"`), and `"nm"` (or `"nmi"` / `"nautical_miles"`). Evaluates distance based on `location.distance_nm` or computes Haversine ground distance from observer coordinates if `distance_nm` is absent. Raises at startup if `min_distance > max_distance`.

#### `vertical_rate_filter`

Keeps only aircraft climbing or descending at a qualifying rate. Either set `mode` for a
common case, or set `min_fpm`/`max_fpm` directly for a custom band.

```toml
[modules.vertical_rate_filter]
mode      = "climbing"   # "climbing", "descending", or "level"
threshold = 200.0        # fpm magnitude used by mode (default 200.0)
```

```toml
[modules.vertical_rate_filter]
min_fpm = 500     # custom band instead of mode (optional)
max_fpm = 2000    # custom band instead of mode (optional)
```

`mode = "climbing"` keeps `vertical_rate_fpm >= threshold`; `"descending"` keeps
`<= -threshold`; `"level"` keeps `-threshold <= vertical_rate_fpm <= threshold`. `mode`
takes priority over `min_fpm`/`max_fpm` when both are set. Aircraft with `UNKNOWN`
vertical rate are excluded as candidates rather than crashing the comparison.

#### `registration_filter`

Keeps only aircraft matching a configured watchlist of tail numbers — useful for "only alert me about these specific airframes" setups.

```toml
[modules.registration_filter]
registrations = ["G-RUKG", "G-RUKC", "G-UJEA"]
```

Filters aircraft based on `airframe.registration` matching the configured watchlist (case-insensitive). Registration arrives from ingest-time enrichment (`tar1090_db` is configured on the `personal_adsb` ingestor), so `registration_filter` can sit anywhere in a chain without needing an enrichment ahead of it.

### Enrichments

Enrichments fill `UNKNOWN` fields in place. Output length always equals input length.

#### `tar1090_db`

Fills `airframe.registration`, `airframe.type_code`, `airframe.type_description` and `airframe.db_flags` from the [tar1090-db](https://github.com/wiedehopf/tar1090-db) aircraft CSV, keyed by ICAO hex. Only fills fields that are currently `None` — never overwrites data the source already supplied. A value from the receiver's own database therefore wins over the CSV, since it is what that receiver actually used.

The CSV's type-code and description columns are carried through as two separate fields, each filled independently: a row with a code but no description still yields a code. They were once collapsed into a single value that preferred the description, which silently discarded the only machine-readable identifier of the two.

**The flags column is a little-endian bit string, not a number.** The character at index *i* is bit *i*, so `'0010'` is PIA (4), `'0001'` is LADD (8) and `'11'` is military|interesting (3). Reading it as decimal or hex silently produces plausible-looking wrong flags for every aircraft, which is worse than having none. `db_flags` is filled when it is currently `None`, including when the parsed value is `0` — "no flags set" is a fact, distinct from "we don't know".

The SQLite index records a schema version in `PRAGMA user_version` (currently **3**). An index built by an older Squawk has a different column count and is rebuilt from the CSV rather than read, so no manual cleanup is needed after an upgrade — and the rebuild fires on a version mismatch alone, without waiting for the CSV to change.

- **Configured on the ingestor, not in a processor chain.** Listed under `modules = [...]` on `[ingestors.personal_adsb]`, so enrichment runs once per aircraft on ingest rather than once per chain per cycle. The enriched values are written into storage, so `data/tracked_aircraft/*.json` files carry registration and aircraft type on disk.
- Downloads `aircraft.csv.gz` automatically on first run and refreshes it every 30 days.
- Cached at `<data_dir>/modules/tar1090_db/aircraft.csv`.
- No config keys of its own.
- **One instance per `[modules.<name>]` block** — the module factory pools instances by name and config, not `tar1090_db` itself. Every chain naming the same block shares one SQLite handle rather than each opening its own.

As noted in project learnings: this only reliably covers US-registered aircraft. European commercial traffic needs the `adsbdb` → callsign-prefix → OpenFlights fallback chain to fill the same fields.

#### `adsbdb`

Fills `airframe.manufacturer`, `airframe.registration`, `airframe.type_description`, `airframe.operator` (registered owner), and the full route block (`route.airline_name`, `route.airline_country`, `route.origin_*`, `route.destination_*`) from [adsbdb.com](https://www.adsbdb.com/).

**Two independent lookups**, because the two halves fail independently:

| Endpoint | Fills | Cache | TTL |
|---|---|---|---|
| `/v0/aircraft/<HEX>` | airframe | `<data_dir>/modules/adsbdb/aircraft/<HEX>.json` | 7 days |
| `/v0/callsign/<CALLSIGN>` | route | `<data_dir>/modules/adsbdb/route/<CALLSIGN>.json` | 1 hour |

**An airframe miss no longer suppresses the route.** These were once a single combined call that was accepted only if it contained an `aircraft` key, so a hex adsbdb did not recognise discarded the route along with it — and a community airframe database is most likely to be missing exactly the newest registrations. Whichever half answers is now applied. The TTLs differ for the same reason the lookups do: an airframe's registration and type are immutable for the life of the aircraft, while a callsign's route is a property of today's flight.

- **Cache-first.** A cache hit costs zero API calls. Definitive misses — a 404, or the aircraft endpoint's `"unknown aircraft"` string response — are cached as not-found markers so they aren't retried every cycle. A timeout, a rate-limit skip or a 500 is *never* recorded as a miss; those stay retryable.
- **In-memory memo (60 seconds).** Sits in front of the disk cache, keyed separately per space. When several chains process the same aircraft in the same cycle, one performs each lookup and the rest reuse its result. Failed and rate-limited lookups are memoised for the same window so they are not retried by every chain.
- **Rate-limited:** enforces adsbdb's published limits (512 calls/60s, 1024 calls/300s) via an in-memory deque; a call that would exceed either window is skipped for the cycle rather than blocking, leaving the field `UNKNOWN` until the next attempt.
- **Skips the route lookup** when `route.callsign` is `UNKNOWN` — there is nothing to look up. The airframe lookup still runs, and airframe data can also arrive via `tar1090_db`.
- **Skips aircraft whose operator has requested suppression**, based on `airframe.db_flags`:

  | Flag | Aircraft lookup | Route lookup | Logged? |
  |---|---|---|---|
  | PIA (4) | skipped | skipped | no line |
  | LADD (8) | **runs** | skipped | `suppressed` |
  | Military (1), Interesting (2) | runs | runs | n/a |

  **PIA** is a temporary Privacy ICAO Address: it identifies no airframe, so neither lookup can succeed and it is treated exactly like a `~` address, log line included. **LADD** is the FAA's Limiting Aircraft Data Displayed programme, and the asymmetry is deliberate: LADD suppresses *flight data*, not the airframe record, which is often present. Skipping both lookups would throw away registration, type and operator that adsbdb gives for free — and this module has already seen the reverse case, where a 404 on the airframe still yielded a full route. The two fail independently and are skipped independently. Military and interesting are descriptive rather than privacy flags and suppress nothing.

  Asking a route API about an aircraft whose operator has formally requested suppression is wrong on its own terms, independent of what it costs. `db_flags` of `None` suppresses nothing: absence of information is not information.
- **Skips non-ICAO addresses entirely.** readsb prefixes an address with `~` when it is not a real ICAO 24-bit address — TIS-B relays and anonymised targets. No airframe exists behind one, so neither lookup is attempted: the call would be a guaranteed miss whose 404 would cache a not-found marker keyed on something that is not an aircraft identifier. These contacts still flow through the pipeline and still display; they simply carry no enrichment. They are also **not** written to the unresolved log — see below.
- **Run this after your filters.** Running it before means every aircraft in range triggers a lookup every cycle, burning through the rate budget for aircraft you're about to discard anyway.
- **One instance per `[modules.<name>]` block** — the module factory pools instances by name and config, not `adsbdb` itself. Eight chains naming `adsbdb` share one cache and one rate limiter, which is what makes the rate limit meaningful across an installation rather than per chain.
- **Licensing:** route data carries a non-commercial licence (David Taylor / Jim Mason). Runtime fetching is fine; the cache is gitignored and must never be committed to the repo.

| Key | Default | Effect |
|---|---|---|
| `log_unresolved` | `false` | Append one JSON line per aircraft whose route adsbdb could not resolve to `<data_dir>/modules/adsbdb/route/unresolved.jsonl` |

`log_unresolved` records what **adsbdb** could not resolve — not what the pipeline as a whole failed to resolve. A later fallback route source may fill some of these in; the distinction matters as soon as a second source exists. Each line carries `at`, `hex`, `callsign`, `registration` and a `reason`, deduplicated on `(hex, callsign)` for the life of the process:

- `no_callsign` — the aircraft has not transmitted identity, so there was nothing to look up.
- `unknown_callsign` — adsbdb answered definitively that it holds no route. A real gap in its data, not fixable here.
- `fetch_failed` — timeout, rate limit or non-404 error. Transient, not a data gap.
- `suppressed` — the aircraft is flagged LADD, so the route was deliberately not requested. Counted because it is a route that plausibly exists and is being withheld — a real gap in what the wall can show. PIA and `~` addresses get no line at all: they were never candidates for anything.

**Expect `no_callsign` to dominate, and filter it out when reading.** It is routinely two-thirds of the file and it is noise: aircraft transmit position before identity, so each is logged once on arrival and then resolves a cycle or two later. A hex logged as `no_callsign` at 18:04 may well be enriched as `SHT19B` by 18:08. Suppressing it would mean tracking whether a hex later resolved — state and logic in service of tidiness — where filtering on read costs nothing:

```
grep unknown_callsign data/modules/adsbdb/route/unresolved.jsonl
```

Two lifetimes are deliberately kept apart here. The on-disk not-found marker **expires**, so a route absent from adsbdb today is retried tomorrow. The log line is written **once per run** per `(hex, callsign)`, checked where the line is written rather than inside the lookup, so a retry that fails again does not add a second line. Collapsing the two would either stop retrying or make the log uncountable. Note that the dedup set lives in memory: restarting Squawk legitimately re-logs aircraft still in range, so a burst of repeats sharing a timestamp is a restart, not a leak.

It is a hand-read diagnostic with no rotation or size bound. If it grows enough to matter, that is itself a finding.

Cache directories, rate limits and TTLs are fixed in code, keyed off `squawk.data_dir`.

### Utility

#### `pass_through`

Returns the aircraft list completely unchanged. Reserves a chain slot during development, or lets you verify the processor is wired up correctly before any real module exists in that position. Not a filter or an enrichment — it's the minimal possible module, useful as scaffolding.

No configuration options.

---

## Displays

Displays sit at (or near) the end of a processor chain, write the list somewhere a human can see, and return it unchanged. Each processor chain specifies its display target via `display = "<name>"` (configured under `[display.<name>]`).


#### `console`

The minimal display — prints the nearest aircraft's registration and type to stdout, or `○ no aircraft` when the list is empty. No config keys. Good for debugging the pipeline over SSH without any browser or hardware involved.

#### `http`

Serves a live web page on a configurable port, auto-updating via Server-Sent Events as new data arrives. Runs in a background thread; `process()` just pokes shared state and returns immediately, so a slow HTTP client can't stall the pipeline.

```toml
[display.http]
port = 7700
```

#### `epaper`

Renders the nearest aircraft to a 250×122 monochrome image, writes it to `<data_dir>/display/epaper/squawk_display.png`, serves that PNG over HTTP for remote preview, and (on the Pi Zero handheld) pushes it to the physical Waveshare 2.13" V4 panel.

```toml
[display.epaper]
port               = 7700    # HTTP port for the PNG preview page
full_refresh_every  = 600    # cycles between full (vs partial) e-paper refreshes
invert              = false  # true = rotate 180° (power lead at top)
```

Only re-renders when the displayed content actually changes (registration, type, operator, callsign, route, distance rounded to 0.1nm, altitude, climb/descend/level state) — this avoids unnecessary e-paper flicker and panel wear.

#### `pushover`

Sends a Pushover push notification for the nearest aircraft, but only once it's fully enriched — airline, registration, callsign, type, **and** both origin and destination all need to be populated, or the notification is silently skipped. Rate-limited per flight identity (hex + callsign) by a configurable cooldown, backed by a small JSON state file on disk.

```toml
[display.pushover]
token            = "xxxxxxxxxxxxxxxxxxxxxxxxxx"
user             = "xxxxxxxxxxxxxxxxxxxxxxxxxx"
cooldown_seconds = 7200   # default 2 hours, per hex+callsign
```

Credentials left as the placeholder `x...x` string are treated as "not configured" and the display quietly no-ops rather than erroring.

