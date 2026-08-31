# Brief: separate adsbdb's airframe and route lookups

**Scope:** `modules/adsbdb.py`, `tests/test_module_adsbdb.py`,
`docs/modules-reference.md`, `config.toml.example`.

**Do not** change `schemas/`, `storage/`, `processor/`, `display/`, `config.py`,
or any other module.

This is a defect fix, not a feature. Behaviour changes only in that aircraft
which previously got nothing now get whatever adsbdb actually holds.

---

## Problem

`_fetch()` builds one URL — `/v0/aircraft/{hex}?callsign={cs}` — and accepts the
response only if it contains an `aircraft` key:

```python
res = data.get("response", data) if isinstance(data, dict) else data
if isinstance(res, dict) and "aircraft" in res:
    return res
return None
```

adsbdb serves airframe and route from two independent endpoints, and the aircraft
endpoint answers for the airframe. When it doesn't know the hex it returns
`{"response": "unknown aircraft"}` — no `aircraft` key, no `flightroute` key — and
the module discards the lot. The route is never requested in a form that could
succeed on its own.

Confirmed live on BAW171, hex `4080C0`:

```
/v0/aircraft/4080C0?callsign=BAW171  →  {"response":"unknown aircraft"}
/v0/callsign/BAW171                  →  full LHR→PIT route, airline, both airports
```

So an **airframe miss takes the route down with it**. The aircraft is G-ZBLL, a
787-10 registered May 2025; a community airframe database is most likely to be
missing exactly the newest registrations, and airline fleets renew continuously.
This is not a one-aircraft problem.

Worth being clear about what this does *not* explain. Of three failures observed
today, only this one is ours. `SHT18A` returns unknown from the callsign endpoint
too — a real gap in adsbdb's route data, unfixable here. `VJT630` is a VistaJet
charter with no scheduled route to hold. Those remain open questions for a future
fallback enricher; this brief fixes only the self-inflicted case.

## Why the coupling is wrong regardless

The two lookups have nothing in common but a hostname.

An airframe's registration, type and manufacturer are immutable for the life of the
aircraft. A callsign's route is a property of today's flight. They fail
independently, they're worth caching for wildly different durations, and — since
`tar1090_db` already supplies registration and type at ingest, for free — the
airframe half is the *less* valuable of the two. The route is what adsbdb is in the
chain for, and it is currently the half held hostage.

---

## Change 1 — two independent fetches

Replace `_fetch()` with two methods, each owning one endpoint:

- `_fetch_aircraft(hex_id)` → `https://api.adsbdb.com/v0/aircraft/{hex}`
- `_fetch_route(callsign)` → `https://api.adsbdb.com/v0/callsign/{cs}`

Each returns its own payload fragment (`{"aircraft": {...}}`,
`{"flightroute": {...}}`), a not-found marker, or `None` on transport failure.
Keep the existing three-way distinction — 200, 404, and everything else — and keep
the current behaviour that only 404 produces a cacheable not-found marker. A
timeout or a 500 must not be recorded as "no such aircraft".

Note the response envelope differs between the two: the aircraft endpoint's unknown
case is the string `"unknown aircraft"` under `response`, not a 404 in every case.
Check both, and treat a string `response` value as a definitive miss.

`process()` calls both, merges the two fragments into one dict, and passes it to
`_apply()`. Skip the route call entirely when the callsign is absent — that is
already the common case for aircraft that have not yet transmitted identity.

## Change 2 — separate cache and memo key spaces

The current cache writes `{hex}.json` and `{callsign}.json` side by side in one
directory, both holding the same merged payload. With two lookups they hold
genuinely different content and must not collide.

Use subdirectories: `aircraft/{hex}.json` and `route/{callsign}.json`.

Memo and key-lock dicts key on `("aircraft", hex)` and `("route", callsign)`
respectively, preserving the existing per-key stampede protection for each space
independently. Two chains cold on the same aircraft still produce one call of each
kind — that property must survive this change, and there are already tests asserting
it that should be adapted rather than deleted.

Two TTLs, replacing the single `_CACHE_TTL_SECONDS`:

- `_AIRCRAFT_TTL_SECONDS = 604800` (7 days) — airframe data does not change
- `_ROUTE_TTL_SECONDS = 3600` (1 hour) — unchanged from current behaviour

The memo TTL stays at 60 seconds for both.

Existing cache files under the module directory are the old merged format and will
simply be ignored once lookups read from the subdirectories. Do not write migration
code; note in the handback that the stale files can be deleted by hand.

The legacy `{callsign}.json` fallback path in `_get_uncached` goes away with this
change — it exists to read caches written by an older version, and those are being
orphaned anyway.

## Change 3 — log unresolved routes

New config key `log_unresolved`, boolean, default `false`. Add to `KEYS`.

When true, an aircraft whose route lookup produced no `flightroute` appends one
JSON line to `route/unresolved.jsonl` in the module directory:

```json
{"at": "...", "hex": "...", "callsign": "...", "registration": "...", "reason": "..."}
```

`reason` distinguishes the cases that matter for diagnosis, because they have
different fixes: `no_callsign` (nothing to look up), `unknown_callsign` (adsbdb
returned a definitive miss — the SHT18A case), and `fetch_failed` (timeout, rate
limit, or non-404 error — transient, not a data gap).

Deduplicate on `(hex, callsign)` for the process lifetime using an in-memory set, or
the file will gain a line per aircraft per cycle. In-memory is sufficient; the log
is a diagnostic that gets read and truncated by hand, not a data store.

**Name it accurately in the docs.** This records what *adsbdb* could not resolve,
not what the pipeline as a whole failed to resolve — a later fallback enricher may
fill some of these in. The distinction will matter as soon as a second route source
exists, and it is cheaper to be precise now than to rename it later.

## Change 4 — `_apply` tolerates a half-empty payload

`_apply` already reads `data.get("aircraft") or {}` and `data.get("flightroute") or
{}`, so it degrades correctly today by accident. Make it correct on purpose: a dict
with only `flightroute` becomes a normal case after this change, not an edge one.

`aircraft.raw.adsbdb` should hold the merged dict, so a consumer sees both halves
where both exist.

One existing line to look at while in there. Every other field is guarded by an
`is None` check so a value from `tar1090_db` is never overwritten, but
`aircraft_type` is not:

```python
if ac.get("type"):
    aircraft.airframe.aircraft_type = ac["type"]
```

That may well be deliberate — adsbdb's type string is more readable than the
tar1090 type code. **Do not change it.** Flag in the handback whether it looks
intentional, and it can be decided separately.

---

## Change 5 — documentation

`docs/modules-reference.md` — the adsbdb entry: two endpoints, two TTLs, two cache
directories, the new `log_unresolved` key, and a note that an airframe miss no
longer suppresses the route.

`config.toml.example` — add `log_unresolved` to the `[modules.adsbdb]` block,
commented, with a line saying what it writes and where.

---

## Change 6 — tests

`tests/test_module_adsbdb.py` needs substantial adaptation: most existing tests mock
a single HTTP call and will need to mock two. Preserve what they assert — the
coverage is good and the concurrency tests especially are worth keeping.

The one that matters most, because it is the defect:

- **an airframe miss still yields a route.** Aircraft endpoint returns
  `{"response": "unknown aircraft"}`, callsign endpoint returns a full flightroute,
  assert `origin_iata` and `destination_iata` are populated. This test fails against
  the current code, which is what makes it the test worth writing first.

Also:

- a route miss still yields airframe data — the mirror case
- both miss: fields stay unknown, one not-found marker written per key space
- airframe cache survives past `_ROUTE_TTL_SECONDS` without a refetch, proving the
  TTLs are genuinely separate
- no callsign means no route call is attempted at all
- concurrent lookups still produce one aircraft call and one route call — adapted
  from the existing stampede tests, not replaced
- a non-404 failure does **not** write a not-found marker
- `log_unresolved = true` writes one line with the right `reason`, and a repeat
  lookup of the same `(hex, callsign)` does not append a second
- `log_unresolved` defaults to false and writes nothing

---

## Explicitly out of scope

- **A fallback route source.** adsb.lol, FR24 and AeroAPI are all under evaluation
  and none is chosen. This brief makes adsbdb do its job properly, which changes how
  much a fallback needs to cover.
- **Changing `aircraft_type`'s unguarded overwrite.** Flag it, don't touch it.
- **Making the TTLs configurable.** Two constants until there is a reason.
- **Rotating or bounding `unresolved.jsonl`.** It is a hand-read diagnostic. If it
  grows enough to matter, that is itself a finding.
- **The category filter**, and anything else to do with panel design.
