# Squawk — Available Plugins

This is a reference catalogue of every plugin shipping in this release: what it does, how it's configured, and where it sits in the pipeline.

```
ingestor → storage ← processor (modules) → display
```

**Ingestors** run independently in background threads and write to storage. **Modules** are the ordered chain the processor runs on every cycle (`processor.modules` in `config.toml`) — by convention these split into **filters** (reduce/reorder the list) and **enrichments** (fill in `UNKNOWN` fields). **Displays** are also modules mechanically, but get their own config key (`processor.display`) and are documented as a separate category since that's what most users will actually swap out.

For the mechanics of writing a new one, see the **Modules Developer Guide** and **Display Modules Developer Guide**. This document is the "what's available" catalogue, not the "how to build one" guide.

## Quick reference

| Name | Category | Config section | Purpose |
|---|---|---|---|
| `personal_adsb` | Ingestor | `[ingestors.personal_adsb]` | Polls your own receiver(s), merges by recency |
| `concorde` | Ingestor | `[ingestors.concorde]` | Synthetic test aircraft, no hardware needed |
| `closest_filter` | Module — Filter | `[modules.closest_filter]` | Reduces to the single nearest aircraft |
| `altitude_filter` | Module — Filter | `[modules.altitude_filter]` | Keeps aircraft within an altitude band |
| `registration_filter` | Module — Filter | `[modules.registration_filter]` | Keeps only a configured tail-number watchlist |
| `tar1090_db` | Module — Enrichment | *(none)* | Fills registration/type from the tar1090 CSV |
| `adsbdb` | Module — Enrichment | *(none)* | Fills manufacturer, owner, and route from adsbdb.com |
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

Modules are the ordered chain named in `processor.modules`; each name maps to a `[modules.<name>]` config table (if it needs one). Every module shares one interface — `process(aircraft) -> aircraft` — so the distinction below is convention, not enforcement.

```toml
[processor]
modules = ["tar1090_db", "registration_filter", "adsbdb", "closest_filter"]
display = "epaper"
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

#### `registration_filter`

Keeps only aircraft matching a configured watchlist of tail numbers — useful for "only alert me about these specific airframes" setups.

```toml
[modules.registration_filter]
registrations = ["G-RUKG", "G-RUKC", "G-UJEA"]
```

Filters aircraft based on `airframe.registration` matching the configured watchlist (case-insensitive). Requires an upstream enrichment (such as `tar1090_db`) that populates `airframe.registration` if the raw feed only provides ICAO hex — has no data source of its own.

### Enrichments

Enrichments fill `UNKNOWN` fields in place. Output length always equals input length.

#### `tar1090_db`

Fills `airframe.registration` and `airframe.aircraft_type` from the [tar1090-db](https://github.com/wiedehopf/tar1090-db) aircraft CSV, keyed by ICAO hex. Only fills fields that are currently `None` — never overwrites data the source already supplied.

- Downloads `aircraft.csv.gz` automatically on first run and refreshes it every 30 days.
- Cached at `<data_dir>/modules/tar1090_db/aircraft.csv`.
- No config keys — it's zero-configuration once enabled in the chain.

As noted in project learnings: this only reliably covers US-registered aircraft. European commercial traffic needs the `adsbdb` → callsign-prefix → OpenFlights fallback chain to fill the same fields.

#### `adsbdb`

Fills `airframe.manufacturer`, `airframe.registration`, `airframe.aircraft_type`, `airframe.operator` (registered owner), and the full route block (`route.airline_name`, `route.airline_country`, `route.origin_*`, `route.destination_*`) via a single combined lookup against [adsbdb.com](https://www.adsbdb.com/).

- **Cache-first:** one JSON file per hex under `<data_dir>/modules/adsbdb/`, 1-hour TTL. A cache hit costs zero API calls. 404s are cached as not-found markers so they aren't retried every cycle.
- **Rate-limited:** enforces adsbdb's published limits (512 calls/60s, 1024 calls/300s) via an in-memory deque; a call that would exceed either window is skipped for the cycle rather than blocking, leaving the field `UNKNOWN` until the next attempt.
- **Skips gracefully** when `route.callsign` is `UNKNOWN` — no callsign means no route lookup, though airframe data can still arrive via `tar1090_db`.
- **Run this after your filters.** Running it before means every aircraft in range triggers a lookup every cycle, burning through the rate budget for aircraft you're about to discard anyway.
- **Licensing:** route data carries a non-commercial licence (David Taylor / Jim Mason). Runtime fetching is fine; the cache is gitignored and must never be committed to the repo.

No config keys — behaviour (cache dir, rate limits, TTL) is fixed in code, keyed off `squawk.data_dir`.

### Utility

#### `pass_through`

Returns the aircraft list completely unchanged. Reserves a chain slot during development, or lets you verify the processor is wired up correctly before any real module exists in that position. Not a filter or an enrichment — it's the minimal possible module, useful as scaffolding.

No configuration options.

---

## Displays

Displays sit at (or near) the end of the chain, write the list somewhere a human can see, and return it unchanged. Only one is active at a time, set via `processor.display`, configured under `[display.<name>]`.

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

