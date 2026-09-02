# Brief: `vrs_route` — route lookup from VRS standing-data

## Goal

Populate `AircraftRoute` from Virtual Radar Server's standing-data — routes
joined against airports — as the primary route source, ahead of `adsbdb` in
the chain. Built on the `data_sources` infrastructure landed in the previous
brief; this is the first real consumer of it.

Context for why this exists: two confirmed cases tonight (`EXS6W`, `THY8MS`)
where adsbdb's route data was wrong and VRS's was right or closer to right.
VRS is a local dataset refreshed daily, CC0-licensed, with a working
correction path back to source (SDM). No caching logic is needed for this
module — the dataset itself is the cache, and it's never more than a day
stale.

---

## Data source: `data_sources/vrs_standing_data.py`

Downloads and keeps current two CSV sets from
`https://github.com/vradarserver/standing-data`:

- `routes/schema-01/**/*.csv` — one file per airline code, e.g.
  `E/EXS-all.csv`. Columns: `Callsign, Code, Number, AirlineCode, AirportCodes`.
  `AirportCodes` is a hyphen-separated list of ICAO codes, two or more —
  confirmed tonight: `LGKR-EGNX` (normal), `LIEO-EGNM-EGPH` (three-stop),
  `EGAA-GCRR-EGAA` (round trip, first and last identical).
- `airports/schema-01/**/*.csv` — one file per two-letter prefix, e.g.
  `HE.csv`. Columns: `Code, Name, ICAO, IATA, Location, CountryISO2,
  Latitude, Longitude, AltitudeFeet`. `IATA` is sometimes empty — confirmed
  tonight on `HE34`.

Files are small per-prefix CSVs, not one bulk file — confirmed tonight with
`RBB-all.csv`, two lines including its header, for a real airline code. The
loader must handle a near-empty file exactly as well as a five-thousand-line
one; no minimum-size assumption anywhere.

### Fetch

Whole-repo zip download (GitHub's repo-archive endpoint, not a git clone —
no history needed and none is offered; the source's own tooling amends one
commit in place rather than accumulating them). Extract `routes/` and
`airports/` only; the other six schema folders are out of scope for this
brief. Unzip to a scratch path under this source's `directory`, then atomic
rename into place — same failure-safety concern as `DiskDriveStorage`'s
temp-and-replace writes, so a failed or partial download can't leave a
half-extracted dataset live.

### Build

Load both CSV sets into SQLite under this source's `directory`, replacing
`tar1090_db`'s single-CSV-single-table shape with two tables and an index on
each join key (`Callsign` for routes, `Code`/`ICAO` for airports — check which
airport column the route's hyphen-separated codes actually match against,
confirm with a real join before committing to the schema).

`_SCHEMA_VERSION` constant, same convention as `tar1090_db` — bump and force a
rebuild if the table shape changes later.

### Refresh policy

VRS publishes once daily via an automated commit at roughly 03:49 UTC
(confirmed from the repo's commit history tonight). This is a "have I got
today's yet" check, not an age threshold — different shape from
`tar1090_db`'s "older than N days," which is exactly why `BaseDataSource`
left policy unspecified.

```toml
[data_sources.vrs]
type             = "vrs_standing_data"
refresh_hour_utc = 6    # check for a newer dataset once past this hour, UTC
```

`ensure_fresh()` compares "have I successfully fetched since the most recent
`refresh_hour_utc` boundary" using a small persisted state (last successful
fetch timestamp, stored in the source's `directory` — a one-line JSON file is
enough). Cheap to call every cycle, as the `BaseDataSource` contract requires;
it should only ever act once per day in practice.

---

## Module: `modules/vrs_route.py`

```toml
[modules.vrs_route]
source = "vrs"
```

`get(cfg, ctx)` resolves `ctx.data_source(cfg["source"])`, calls
`ensure_fresh()`, and holds the SQLite connection for lookups — same
threading pattern as `Tar1090DbEnricher`/`SQLiteTarDb` (thread-local
connections, since module instances are shared across chains running in
separate threads).

`process()`: for each aircraft with a `route.callsign` and any `UNKNOWN` route
field, look up the callsign in the routes table. On a hit, split
`AirportCodes` on `-`. Use the **first and last** entries as origin and
destination — the two-leg case the wall displays. Multi-stop routes
(`LIEO-EGNM-EGPH`) reduce to first/last for now; the middle stop is discarded,
not modelled. **Do not treat `first == last` as an error** — `EGAA-GCRR-EGAA`
is a real positioning round-trip, confirmed tonight, and must resolve to
identical origin and destination rather than being rejected or logged as
suspect.

Join each ICAO code against the airports table for IATA, `Location`
(→ `route.origin_name`/`destination_name`... check whether `Location` maps
better to a new field or reuses the existing `origin_name`/`destination_name`
slot before assuming; `Location` is closer to "municipality" than "airport
name" per tonight's `adsbdb` municipality work — if it's the same concept,
consider whether it should populate the same field `adsbdb` populates, so the
display doesn't care which module answered) and `CountryISO2` (resolve
against `countries.csv` for the full name, or store the ISO code and let
display resolve it — decide based on what `adsbdb`'s existing country fields
actually store; match that shape rather than introducing a second
convention).

Field writes are guarded exactly like `adsbdb`'s `_apply` — `if
aircraft.route.origin_iata is None and ...` — so a value already present
survives. This matters here specifically because of chain order: `vrs_route`
runs first, `adsbdb` second, and `adsbdb`'s job becomes filling only what VRS
didn't cover.

An airport code with no matching row, or an IATA-less airport (`HE34`-style),
degrades the same way missing route data already does elsewhere in this
codebase — the field stays `UNKNOWN`, nothing raises.

### Unresolved logging

Same shape as `adsbdb`'s `log_unresolved` — a callsign with no matching row in
the routes table gets logged, distinguishing reasons the way `adsbdb`
already does (`no_callsign`, `unknown_callsign`). This is the measurement
that decides, later, whether a paid API tier is worth building at all: what
VRS doesn't cover is GA, charter, and the sporadic traffic categories
identified earlier tonight — this log is how you'll know the real shape and
size of that gap rather than guessing.

---

## Wiring

```toml
[processors.high_bands]
modules = ["band_closest", "vrs_route", "adsbdb"]

[processors.low_bands]
modules = ["band_closest", "vrs_route", "adsbdb"]
```

`vrs_route` before `adsbdb`, both after `band_closest` — same reasoning as
tonight's `adsbdb` placement: only the selected aircraft pay for either
lookup.

**Do not remove `adsbdb` from the chain.** It remains the fallback for
callsigns VRS has no route for at all — the sporadic/GA/charter traffic VRS's
coverage description already excludes. Whether adsbdb's *route* half is worth
keeping once the unresolved log has a few days of data is a decision for
later, not this brief.

---

## Tests

New `tests/test_vrs_route.py` and a data-source-level
`tests/test_vrs_standing_data.py` (or folded into one file — match whatever
convention `test_data_sources.py` established for source-type tests).

Data source:
1. Zip download extracts only `routes/` and `airports/`, ignores the other
   six folders.
2. A partial/failed download doesn't leave a half-extracted dataset live —
   temp-and-replace proven the same way `DiskDriveStorage`'s atomic write is
   tested.
3. `ensure_fresh()` fetches once per UTC day after `refresh_hour_utc`, not
   more — mirror the interval tests from the `data_sources` brief, adapted to
   a wall-clock boundary instead of an elapsed-time one.
4. A two-line CSV (the `RBB` case) loads without error and is queryable.
5. `_SCHEMA_VERSION` mismatch forces a rebuild, same pattern as `tar1090_db`.

Module:
6. Two-airport route resolves origin and destination correctly.
7. Three-airport route (`LIEO-EGNM-EGPH`) resolves to first/last, middle
   discarded.
8. Round-trip route (`EGAA-GCRR-EGAA`) resolves with origin equal to
   destination — explicitly not treated as an error.
9. Unknown callsign leaves the route `UNKNOWN`, logs `unknown_callsign`.
10. No callsign at all — logs `no_callsign`, consistent with `adsbdb`'s
    existing category.
11. Airport code with no IATA (`HE34`-style) — the IATA field stays
    `UNKNOWN`, everything else populates.
12. Field already populated (simulating chain order) is not overwritten —
    the guard, proven the same way `adsbdb`'s guarded fields are tested.
13. Airport code with no matching row at all — degrades to `UNKNOWN`, no
    exception.
14. Two chains referencing `vrs_route` with the same `source = "vrs"` share
    one `data_sources` instance and therefore one download — pooling proof,
    same shape as the `data_sources` brief's own test 4.

---

## Docs

`docs/modules-reference.md` — new entry for `vrs_route`, its position ahead
of `adsbdb` in a chain, the first/last multi-stop rule, and the round-trip
case explicitly called out so nobody "fixes" it later.

`docs/data-sources-guide.md` — `vrs_standing_data` as the first worked
example of a concrete `BaseDataSource`, since the guide currently only
describes the contract in the abstract.
