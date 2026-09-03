# Brief: `vrs_standing_data` — full standing-data ingest into SQLite

## Goal

Build (not just route/airports as originally scoped in `brief-vrs-route.md`)
a `BaseDataSource` that downloads the *entire* VRS standing-data repo and
loads all eight schema-01 datasets into one SQLite file. Today's consumer is
still just routes, but every dataset goes in now rather than coming back to
touch this module once for each future consumer.

This supersedes the "Data source" section of `brief-vrs-route.md` — that
section scoped only `routes/` and `airports/`. Everything else in that brief
(the `vrs_route` module itself, wiring, its own tests) is unchanged, except
it now reads from a shared multi-table DB instead of a two-table
routes-and-airports-only one.

Confirmed from tonight's `standing-data-main.txt` (`ls -lR` of the repo):

- Eight top-level dataset folders, each containing `schema-01/`: `aircraft`,
  `airlines`, `airports`, `code-blocks`, `countries`, `model-type`,
  `registration-prefixes`, `routes`.
- `aircraft/schema-01` is sharded three levels deep — one hex digit, then two
  hex digits, then a 3-character filename (e.g. `0/00/008.csv`). By far the
  largest dataset by file count.
- `routes/schema-01` is sharded by first letter, one file per airline code —
  **except** large airlines, which split into numbered chunks: `VLG-1.csv`
  through `VLG-9.csv`, `WZZ-0.csv` through `WZZ-9.csv`. No `VLG-all.csv`
  exists once an airline is chunked.
- `airports/schema-01` is sharded by two-letter prefix (small files, matches
  the `3F.csv`/`HE.csv`-style samples already reviewed).

**Loader implication:** glob `<dataset>/schema-01/**/*.csv` per dataset.
Never assume a filename pattern, an `-all` suffix, or a file count as a size
proxy — a dataset's row count and its file count are unrelated (single
`WZZ-*` files run tens of thousands of rows; many `aircraft` shard files are
under ten).

---

## Data source: `data_sources/vrs_standing_data.py`

### Fetch

```
https://codeload.github.com/vradarserver/standing-data/zip/refs/heads/main
```

Same mechanism already agreed for the routes-only version: whole-repo zip
via GitHub's archive endpoint (`codeload.github.com`, already on the
egress allow-list), not a git clone — a plain `requests.get()` with
`stream=True`, same as `tar1090_db._download`'s pattern, just writing the
zip to a temp path instead of a gzip'd CSV. Extract to a scratch path under
this source's `directory`, then atomic rename — same temp-and-replace
pattern as `DiskDriveStorage`, so a failed or partial download never leaves
a half-extracted tree live. Discard the extracted CSVs once the SQLite
build succeeds; they're not needed after that point and re-extracting on
the next refresh is cheap enough not to justify keeping them around.

Confirmed tonight: `200`, `content-type: application/zip`,
`content-disposition: attachment; filename=standing-data-main.zip` — the
`standing-data-main` root folder name matches the `ls -lR` reviewed
earlier, so this is the same tree the schema above was built against.

### Build

One SQLite file, eight tables, one build pass, one transaction. Table
shapes below are taken directly from tonight's sample files — column names
snake_cased for SQL, source CSV header preserved in a comment above each
`CREATE TABLE` so a future schema diff is easy to spot.

| Table | Source folder | Columns | Suggested index |
|---|---|---|---|
| `aircraft` | `aircraft` | `hex TEXT PRIMARY KEY, registration TEXT, model_icao TEXT, manufacturer TEXT, model TEXT, manufacturer_and_model TEXT, is_private_operator INTEGER, operator TEXT, airline_code TEXT, serial_number TEXT, year_built INTEGER` | PK on `hex` |
| `airlines` | `airlines` | `code TEXT, name TEXT, icao TEXT, iata TEXT, positioning_flight_pattern TEXT, charter_flight_pattern TEXT` | index on `code`, `icao` |
| `airports` | `airports` | `code TEXT, name TEXT, icao TEXT, iata TEXT, location TEXT, country_iso2 TEXT, latitude REAL, longitude REAL, altitude_feet INTEGER` | index on `code`, `icao` (confirm which one routes' `AirportCodes` actually joins against before `vrs_route` locks its query — see original brief's open question) |
| `code_blocks` | `code-blocks` | `start TEXT, finish TEXT, count INTEGER, bitmask TEXT, significant_bitmask TEXT, is_military INTEGER, country_iso2 TEXT` | index on `start`, `finish` |
| `countries` | `countries` | `iso TEXT PRIMARY KEY, name TEXT` | PK on `iso` |
| `model_type` | `model-type` | `icao TEXT, manufacturer TEXT, model TEXT, engines INTEGER, engine_type_code TEXT, engine_placement_code TEXT, species_code TEXT, wake_turbulence_code TEXT, is_active INTEGER` | index on `icao` |
| `registration_prefixes` | `registration-prefixes` | `prefix TEXT, country_iso2 TEXT, has_hyphen INTEGER, decode_full_regex TEXT, decode_no_hyphen_regex TEXT, format_template TEXT` | index on `prefix` |
| `routes` | `routes` | `callsign TEXT, code TEXT, number INTEGER, airline_code TEXT, airport_codes TEXT` | index on `callsign` (today's only consumer) |

Build process, same pragmas and batching as `tar1090_db._build_sqlite_db`:
`PRAGMA synchronous = OFF`, `PRAGMA journal_mode = MEMORY`, `executemany`
in batches, one transaction, build to a `.tmp` file and `Path.replace()`
into place atomically so a reader never sees a half-built DB.

`_SCHEMA_VERSION` constant via `PRAGMA user_version`, same convention as
`tar1090_db` — bump and force a rebuild if any table's shape changes later.
One version number covers all eight tables; there's no scenario where only
one table's shape changes independently of a fresh repo pull.

### Refresh policy

Weekly, by elapsed days — **not** the hour-of-day "have I got today's yet"
check the original `brief-vrs-route.md` specified. That was written when the
plan was routes/airports only and matched VRS's daily publish cadence;
tonight's decision is that a week is enough headroom for a dataset this
size, and elapsed-days is the simpler, already-proven shape
(`tar1090_db._needs_refresh` does exactly this).

No separate state file: reuse the built SQLite file's own mtime as the
freshness clock, same as `tar1090_db` uses the CSV's mtime. One less thing
to keep in sync, and it's already atomic-replace-safe.

```toml
[data_sources.vrs]
type         = "vrs_standing_data"
refresh_days = 7
```

`KEYS = {"refresh_days"}`, `_validate_refresh_days` copied verbatim in
shape from `tar1090_db`'s (positive int, default 7 if unset, reject bool/
non-int/non-positive).

`ensure_fresh()`: if the DB file doesn't exist, or is older than
`refresh_days`, or its `PRAGMA user_version` doesn't match
`_SCHEMA_VERSION` — download and rebuild. Otherwise no-op. Cheap to call
every cycle per the `BaseDataSource` contract; in practice fires once a
week.

### Query surface

A thin `SQLiteVrsDb` wrapper, thread-local connections, same shape as
`SQLiteTarDb` — module instances are shared across chains running in
separate threads, so this can't assume single-threaded access. Expose one
method per table needed by a consumer as those consumers get built; only
`get_route(callsign)` is needed today for `vrs_route`. Don't pre-build
query methods for the other seven tables speculatively — that's the kind of
premature generalisation the project avoids elsewhere; add them when a
module actually needs them.

---

## Tests

New `tests/test_vrs_standing_data.py`:

1. Zip download extracts all eight `schema-01` trees, not just two.
2. A partial/failed download doesn't leave a half-extracted tree or a
   half-built DB live — temp-and-replace proven the same way
   `DiskDriveStorage`'s atomic write and `tar1090_db`'s DB build already are.
3. `ensure_fresh()` refreshes when the DB is older than `refresh_days`, not
   more often — mirror `tar1090_db`'s age-based interval tests.
4. `_SCHEMA_VERSION` mismatch forces a rebuild.
5. A dataset with a chunked/split file set (simulate the `VLG`/`WZZ`
   numbered-chunk case) loads every chunk into one logical table — this is
   the one genuinely new risk in tonight's design, since nothing in the
   codebase has needed to concatenate split source files before.
6. A near-empty shard file (the `RBB`, two-line case from tonight) loads
   without error.
7. All eight tables are queryable after a build — one row-count sanity
   check per table is enough; this test doesn't need to validate every
   column's data, just that the table exists and isn't empty for a real
   sample tree.
8. Two chains referencing `source = "vrs"` share one instance and therefore
   one download/build — pooling proof, same shape as the `data_sources`
   brief's own pooling test.

---

## Docs

`docs/data-sources-guide.md` — update the `vrs_standing_data` example to
reflect all eight tables, not two. Note the weekly/elapsed-days refresh
policy explicitly as a *different* shape from tar1090_db's identical-looking
policy having been reused here, since the guide's whole point is documenting
which policy shape a new source type should copy.

---

## Follow-on

`brief-vrs-route.md`'s `vrs_route` module section is otherwise unchanged,
but its `ensure_fresh()` call now hits this shared multi-table source rather
than a routes-and-airports-only one, and its SQL reads from the `routes`
table shape defined here rather than redefining its own.
