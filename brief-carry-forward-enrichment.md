# Brief: carry forward stable enrichment across poll cycles

## Goal

Stop `tar1090_db` and `vrs_route` (and any future hex/callsign-keyed
enrichment module) from re-deriving `airframe`/`route` from scratch every
poll cycle for an aircraft already enriched. No new module contract, no
per-module classification — every enrichment module already guards its
writes with `if field is None`. The only missing piece is that the object
handed to those modules is rebuilt blank every cycle. Fix that one point,
and the existing guards do the rest for free.

`STALE_SECONDS` (`storage/__init__.py`, currently `60`) is unchanged —
already the right shape for this: it's what already defines "gone."

## Where this sits

`storage` is already a persistent, hex-keyed, staleness-aware store —
`DiskDriveStorage.retrieve_aircraft(hex)` already returns `None` for
absent *or* stale records. It's the "pot" — it's just never consulted
until the very end of the cycle (`save_aircraft_array`). This brief adds
one read at the *start* of the cycle instead.

In `ingestor/personal_adsb/ingestor.py::run()`:

```python
merged   = _merge_snapshots(snapshots)
aircraft = _build_aircraft(merged)
aircraft = _carry_forward_enrichment(aircraft, storage)   # <-- new

for m in ingest_modules:
    aircraft = m.process(aircraft)

storage.save_aircraft_array(aircraft)
```

`storage` is already in scope in `run()` — no new plumbing to pass it
anywhere.

## `_carry_forward_enrichment(aircraft, storage)`

For each freshly-built `Aircraft`:

1. `existing = storage.retrieve_aircraft(a.meta.icao_hex)`. `None` (absent
   or expired past `STALE_SECONDS`) → leave `a` untouched; this is a new
   sighting (or one old enough to treat as a fresh flight), and every
   enrichment module runs in full, same as today.
2. `existing` found → reconstruct via `aircraft_from_dict` (or read its
   `airframe`/`route` sub-dicts directly — either works, pick whichever
   is less code).
3. **`airframe`**: merge every field where `a.airframe.<field> is None`
   by copying `existing.airframe.<field>` across, unconditionally. Keyed
   by hex, not callsign — always safe. This covers `registration`,
   `type_code`, `type_description`, `category`, `db_flags`,
   `manufacturer`, `operator`. (Some of these — `category`, `db_flags`,
   `registration` — may already be freshly populated by the converter
   straight from this cycle's raw ADS-B; the `is None` guard means raw
   data always wins over a carried-forward value, which is correct —
   raw is more current than anything cached.)
4. **`route`**: **only merge if `a.route.callsign == existing.route.callsign`**
   (including the case where both are `None` — no callsign, nothing to
   distinguish flights by, safe to carry forward). If the callsigns
   differ, the aircraft has moved on to a different flight since the pot
   record was written and its old route data must **not** carry over —
   leave `a.route` exactly as freshly converted (`callsign`/`squawk_code`
   from raw, everything else `UNKNOWN`), so `vrs_route` re-resolves the
   new callsign from scratch. This is the one genuinely load-bearing
   rule in this brief; get the test for it right before anything else.
5. Never touch `meta`, `location`, `direction` — those are always this
   cycle's fresh raw data, unconditionally.

## Consequence for modules

None — `tar1090_db.py` and `vrs_route.py` are both unchanged. Once step 3
has already filled `a.airframe.registration`, `tar1090_db`'s existing `if
a.airframe.registration is None and reg:` guard already skips its own
work. Same for every `vrs_route` field once step 4 has filled `route`.
Confirm this with a test that actually counts calls, not just checks the
end state — the point of this brief is a lookup that doesn't happen, so
the test needs to prove absence, not just correctness of what's present.

## Tests

New `tests/test_ingest_enrichment_carryforward.py` (or fold into
`tests/test_ingest_modules.py` if that's the more natural home — match
whatever `test_personal_adsb.py`/`test_ingest_modules.py` already do for
ingest-loop-level tests):

1. First sighting of a hex (nothing in storage) — enrichment modules run
   in full, `_carry_forward_enrichment` is a no-op.
2. Same hex, same callsign, second cycle — `airframe` and `route` fields
   present in storage are carried forward; `vrs_route`'s `get_route()` is
   **not called again** (assert call count, not just field values).
3. Same hex, **different callsign**, second cycle (still within
   `STALE_SECONDS`) — `route` is **not** carried forward; `vrs_route`
   runs again for the new callsign. `airframe` still carries forward
   (hex hasn't changed).
4. Hex present in storage but past `STALE_SECONDS` — treated as absent;
   full re-enrichment, nothing merged.
5. Raw ADS-B supplies `airframe.registration` directly this cycle (the
   `raw.get("r")` case) — the raw value wins over a different
   carried-forward value, per the `is None` guard ordering.
6. A field `UNKNOWN` in both the fresh object and the stored record stays
   `UNKNOWN` — merging doesn't invent data.
7. Callsign `None` on both sides (no flight number transmitted either
   cycle) — treated as a match, `route` fields still carry forward
   (covers aircraft broadcasting position but no `flight` field).

## Docs

`docs/storage-guide.md` and/or `docs/primary_ingestor.md` — note that
`storage.retrieve_aircraft()` now has a second caller (the ingest loop
itself, not just the display/processor read path), and that this is what
makes `STALE_SECONDS` do double duty: both "when does an aircraft
disappear from the display" and "how long do we trust its enrichment
without redoing it." One constant, two jobs — worth being explicit that
this is deliberate, not a coincidence, so nobody splits it into two
constants later without noticing the connection.
