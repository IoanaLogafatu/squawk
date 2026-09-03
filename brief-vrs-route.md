# Brief: `vrs_route` — route lookup from VRS standing-data (rev 2)

Supersedes the module section of the original `brief-vrs-route.md`. The data
source is no longer this module's concern — `vrs_standing_data` already
ships (539 tests passing) with all eight tables in one SQLite file, weekly
refresh, and `SQLiteVrsDb.get_route(callsign)` as its only exposed accessor
so far. This brief adds the accessors `vrs_route` actually needs and builds
the module itself.

## Goal

Populate `AircraftRoute` from the already-landed VRS dataset — routes joined
against airports and countries. `adsbdb` is being removed from both
processor chains for this session; what happens to it is not this brief's
concern. Writing this module now is also what actually exercises the data
source's download for the first time outside its own tests: no module
references `source = "vrs"` yet, so the block in `config.toml` is currently
dormant.

---

## New accessors on `SQLiteVrsDb` (`data_sources/vrs_standing_data.py`)

`get_route(callsign)` already exists. Add, following the same thread-local
connection pattern:

- `get_airport(icao: str)` → row from the `airports` table, keyed on
  `airports.icao` — **confirmed tonight** this is the correct join column:
  `routes.airport_codes` values (`SBGR`, `SGAS`, `EGNX`) are ICAO codes, not
  the `Code` column, which in at least one real shard (`3F.csv`) happens to
  equal `icao` but is not guaranteed to in general (don't rely on that
  coincidence).
- `get_country(iso2: str)` → row from `countries`, keyed on `countries.iso`.
- `get_airline(code: str)` → row from `airlines`, keyed on `airlines.code`
  — used for `airline_name`, joined against `routes.airline_code`.

No accessor for `aircraft`, `code_blocks`, `model_type`, or
`registration_prefixes` — nothing needs them yet; don't add speculatively.

---

## Module: `modules/vrs_route.py`

```toml
[modules.vrs_route]
source = "vrs"
```

`get(cfg, ctx)` resolves `ctx.data_source(cfg["source"])`, calls
`ensure_fresh()` — this is what actually triggers tonight's real download —
and holds the `SQLiteVrsDb` for lookups.

### `process()`

For each aircraft with `route.callsign` and any `UNKNOWN` route field, call
`get_route(callsign)`.

**On a hit:**

1. Split `airport_codes` on `-`. Use **first and last** as origin/destination
   — the two-leg case the wall displays. Multi-stop routes (`SBGL-SAEZ-SGAS`,
   confirmed in tonight's `LAPall.csv` sample) reduce to first/last; the
   middle stop is discarded, not modelled.
2. **Do not treat `first == last` as an error** — round-trip routes are real
   (confirmed earlier as `EGAA-GCRR-EGAA`) and must resolve to identical
   origin and destination, not be rejected or logged as suspect.
3. For each of origin/destination: `get_airport(icao)`. On a hit, fill
   `*_iata` (from `airports.iata`, may be empty/`None` — leave `UNKNOWN`,
   don't error), `*_name` (from `airports.name`), `*_municipality` (from
   `airports.location` — confirmed tonight this is a city, e.g. "Fort
   Myers", not a repeat of the airport name). For `*_country`: look up
   `airports.country_iso2` against `get_country()` and use `.name`; if
   either lookup misses, leave `UNKNOWN` rather than storing the raw ISO
   code — `AircraftRoute.origin_country`'s existing convention is a full
   name (`"Spain"`), matching what `adsbdb` already writes, so this must
   match that shape rather than introduce a second one.
4. `flight_number`: **verify before implementing.** `LAPall.csv` shows
   `Callsign` as `Code + Number` concatenated with `Code` at 3 characters
   (`LAP` + `1300` = `LAP1300`), which is ICAO-style, not the IATA-style
   2-letter-prefix format `AircraftRoute.flight_number`'s own docstring
   expects (`"BA117"`). Before wiring this field, pull a real shard for a
   well-known airline with a distinct IATA code (e.g. `VLG` = Vueling,
   IATA `VY`) and check whether `Code` is ever 2 characters there. If `Code`
   is uniformly ICAO-style across the dataset, `flight_number` should be
   left **entirely unset by everything** — `adsbdb` is out of the chain
   (see Wiring below), so there's no fallback to hand this off to right
   now. That's a real, visible gap, not a deferred one — don't write an
   ICAO-shaped value into an IATA-shaped field just to fill it.
5. `airline_name`: `get_airline(routes.airline_code)` → `.name`, guarded the
   same way as everything else.
6. `airline_country`: no source for this in VRS data (the `airlines` table
   has no country column). Leave `UNKNOWN` — no fallback currently fills
   it, same reasoning as `flight_number` above.

**Field writes are guarded** exactly like `adsbdb`'s `_apply` — `if
aircraft.route.origin_iata is None and ...` — so a value already present
survives. This matters specifically because of chain order: `vrs_route`
runs first, `adsbdb` second, and `adsbdb`'s job becomes filling only what
VRS didn't cover (plus `flight_number`/`airline_country` entirely, per
above).

An airport code with no matching row, a country with no matching row, or an
IATA-less airport degrade the same way missing data already does elsewhere
— the field stays `UNKNOWN`, nothing raises.

### Unresolved logging

Same shape as `adsbdb`'s `log_unresolved` — a callsign with no matching row
in `routes` gets logged, distinguishing `no_callsign` / `unknown_callsign`
the way `adsbdb` already did. This matters more than it would have under
the original plan: with `adsbdb` out of the chain entirely, this log is now
the **only** visibility into what VRS-only route coverage is missing —
there's no automatic fallback catching the gap silently. This is the
evidence the "maybe use adsbdb" decision gets made from, not a nice-to-have
metric.

---

## Debug logging

New pattern — nothing else in the codebase has level-gated logging today;
`adsbdb`/`tar1090_db` both `print()` unconditionally. Scoped to `vrs_route`
only for this brief, not retrofitted onto other modules — that's separate
work for whenever those modules are next touched, not this session's job.

```toml
[modules.vrs_route]
source    = "vrs"
log_level = "errors"   # "none" | "errors" | "verbose" — default "errors"
```

- **`none`** — silent. No per-lookup output at all.
- **`errors`** (default) — a lookup that fails or misses prints one line.
  Covers similar ground to the unresolved log below, but to console/stdout
  rather than the persistent categorized file — the two are separate
  mechanisms serving different purposes and this brief doesn't merge them.
- **`verbose`** — every lookup prints a line, hit or miss.

Format matches the existing `"  <module>: ..."` two-space-indent
convention already used by `adsbdb`/`tar1090_db`:

```
  vrs_route: callsign BDEH4DE returned LGW - LHR
  vrs_route: callsign BDEH4DE unknown — no route
```

`_validate_log_level(value)`: `None` → `"errors"`; otherwise must be one of
the three literal strings or raise — same rejection-not-silent-default
shape as `tar1090_db`'s `_validate_refresh_days`.

`KEYS = {"source", "log_level"}`.

---

## Wiring

```toml
[processors.high_bands]
modules = ["band_closest", "vrs_route"]

[processors.low_bands]
modules = ["band_closest", "vrs_route"]
```

`adsbdb` is removed from both chains — out of scope for this brief, not
a decision made here. Its absence means no fallback exists right now for
routes VRS doesn't cover, or for `flight_number`/`airline_country` (see
below) — that's a real gap while this runs, not a hidden one.

---

## `config.toml`

Uncomment the existing `[data_sources.vrs]` block (already present,
currently dormant) and add:

```toml
[modules.vrs_route]
source = "vrs"
```

This is what actually turns the dormant block live — the comment above it
already explains why nothing downloads until a module names `source =
"vrs"`.

---

## Tests

New `tests/test_vrs_route.py`:

1. Two-airport route resolves origin and destination correctly.
2. Three-airport route (`SBGL-SAEZ-SGAS` shape) resolves to first/last,
   middle discarded.
3. Round-trip route (`first == last`) resolves with origin equal to
   destination — explicitly not an error.
4. Unknown callsign leaves the route `UNKNOWN`, logs `unknown_callsign`.
5. No callsign at all — logs `no_callsign`.
6. Airport with no IATA — `*_iata` stays `UNKNOWN`, everything else
   populates.
7. Airport code with no matching row at all — degrades to `UNKNOWN`, no
   exception.
8. Country ISO with no matching row — `*_country` stays `UNKNOWN` rather
   than falling back to the raw ISO code.
9. `airline_name` resolves via `routes.airline_code` → `airlines.code`.
10. Field already populated (simulating chain order) is not overwritten.
11. `flight_number` and `airline_country` are **not** written by this
    module regardless of outcome — pending the verification step above; if
    that verification instead shows `Code` genuinely is IATA-style for some
    airlines, this test (and item 4 in `process()`) needs revisiting before
    merge, not after.
12. Two chains referencing `vrs_route` with the same `source = "vrs"` share
    one data source instance — pooling proof.

`log_level` (see Debug logging above):

13. `"none"` — no output on hit or miss.
14. `"errors"` (default, including unset) — miss prints, hit does not.
15. `"verbose"` — both hit and miss print, exact format matched.
16. Invalid value raises at construction, same shape as an invalid
    `refresh_days`.

New accessor tests in `tests/test_vrs_standing_data.py`:

17. `get_airport()` keyed on `icao`, not `code`.
18. `get_country()` and `get_airline()` return `None` on miss, a row on hit.

---

## Docs

`docs/modules-reference.md` — new entry for `vrs_route`: the first/last
multi-stop rule, the round-trip case, and — once resolved — whichever way
the `flight_number` verification lands, stated explicitly so nobody "fixes"
it later in the wrong direction. Also note that `adsbdb` is currently
absent from both processor chains entirely (not just superseded for
routes), so the next person reading the config doesn't assume it's still
providing airframe-gap coverage.
