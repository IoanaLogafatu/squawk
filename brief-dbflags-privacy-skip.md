# Brief: capture `dbFlags`, and stop asking about suppressed aircraft

**Scope:** `schemas/aircraft.py`, `ingestor/personal_adsb/converter.py`,
`modules/tar1090_db.py`, `modules/adsbdb.py`, `docs/readsb_aircraft_fields.md`,
`docs/modules-reference.md`, `tests/test_schema_aircraft.py`,
`tests/test_personal_adsb.py`, `tests/test_tar1090_db.py`,
`tests/test_module_adsbdb.py`.

**Do not** change `display/`, `storage/`, `processor/`, `config.py`, `main.py`, or
any filter module. No new filter module — a `flag_filter` belongs with the panel
work and depends on this landing first.

---

## Problem

**The flag is read twice and discarded twice.** `docs/readsb_aircraft_fields.md`
already documents `dbFlags` as a bitfield — **1 = military, 2 = interesting,
4 = PIA, 8 = LADD** — and notes it is DB-enrichment only, present when the receiver
runs with `--db-file`. The converter never reads it. `tar1090_db` parses the CSV's
`flags` column into `row[3]` and then uses only `row[2]` and `row[4]`.

This is the same oversight as the type code: both facts sit in the source, one is
kept, one is dropped.

**Two of those flags mean "do not look this aircraft up."**

- **LADD** — the FAA's Limiting Aircraft Data Displayed programme. The owner has
  formally requested that flight data not be displayed. Squawk currently asks
  adsbdb for the route anyway, on every cache-miss cycle, for as long as the
  aircraft is in range.
- **PIA** — Privacy ICAO Address. A temporary address allocated to obscure the
  aircraft's identity. The hex is not a stable identifier, so neither lookup can
  succeed.

Asking a route API about an aircraft whose operator has formally requested
suppression is wrong on its own terms, independent of what it costs. This lands as
a correctness fix, not an optimisation.

The volume argument is deliberately not being made. Squawk is open source and runs
wherever someone puts a receiver; LADD is a US programme, so a UK installation sees
few and a Texan one sees many. Sizing the change against one location's traffic
would be the wrong basis for a decision that applies everywhere.

---

## Change 1 — schema

`Airframe` gains one field:

```python
db_flags: Optional[int] = UNKNOWN   # tar1090 bitfield: 1 military, 2 interesting, 4 PIA, 8 LADD
```

Store the integer, not decoded booleans. It is what both sources supply, it round
trips cleanly, and callers that care about one bit can test it. Do **not** add
derived properties or a decoded set — there is exactly one consumer in this brief
and a `flag_filter` later may want a different shape than whatever gets guessed now.

Named constants for the bits belong in `modules/adsbdb.py` alongside the code that
tests them, not in the schema. `_FLAG_PIA = 4` and `_FLAG_LADD = 8` are enough; do
not define the two this brief does not use.

## Change 2 — converter

One line, from a field already in the raw record:

```python
db_flags = raw.get("dbFlags", UNKNOWN),
```

Note this is absent when a receiver runs without `--db-file`, which is a normal
configuration. `None` must be handled as "unknown", never as zero — the distinction
between "no flags set" and "we don't know" matters, because the second must not be
treated as permission to look up.

## Change 3 — `tar1090_db` keeps the flags column

The database gained a third column in the airframe-fields brief. It gains a fourth:
`(registration, type_code, description, flags)`, from `row[3]`.

The CSV column is a hex string in tar1090's format, not a decimal integer — check
what it actually contains before assuming, and parse accordingly. Get this wrong and
every aircraft acquires plausible-looking wrong flags, which is worse than having
none.

Fill `db_flags` only when it is currently `None`, consistent with every other field
this module writes. A value from the receiver's own database wins over the CSV,
since it is what that receiver actually used.

This changes the persisted schema again, so bump `_SCHEMA_VERSION` to 3. The
`user_version` check added last time is exactly the mechanism for this and should
need no new machinery.

## Change 4 — adsbdb skips suppressed aircraft

In `process()`, alongside the existing `~` guard:

- **PIA set** — skip both lookups. The address is temporary and identifies nothing.
  Treat it exactly as `~` is treated, including writing no unresolved-log line.
- **LADD set** — skip the *route* lookup only. Keep the aircraft lookup.

That asymmetry is the point of the change and should carry a comment. LADD suppresses
flight data; the airframe record is not suppressed and may well be present. Blanket
skipping would throw away registration, type and operator that adsbdb would have
given for free — and this project has already seen the reverse case, where a 404 on
the airframe still yielded a full route. The two lookups fail independently and must
be skipped independently.

- **Military and interesting** — skip nothing. They are descriptive, not privacy
  flags, and a military aircraft's airframe is often in adsbdb.

**Log LADD route skips**, with a new reason: `suppressed`. This is different from
`~` and PIA, which get no line at all. The distinction is that LADD is a route that
plausibly exists and is being deliberately withheld — worth counting, because it is
a real gap in what the wall can show. A PIA or `~` address was never a candidate for
anything.

`db_flags` being `None` means skip nothing. Absence of information is not
information.

---

## Change 5 — documentation

`docs/readsb_aircraft_fields.md` — the `dbFlags` row already exists; note that
Squawk now captures it and what it is used for.

`docs/modules-reference.md` — the adsbdb entry: which flags suppress which lookup and
why LADD is asymmetric. The `tar1090_db` entry: the fourth column and the schema
version bump.

---

## Change 6 — tests

- schema: `db_flags` defaults to `UNKNOWN` and survives a JSON round trip
- converter: populates from `dbFlags`; is `None` when the field is absent
- `tar1090_db`: fills `db_flags` from the CSV column, parsed correctly; leaves an
  existing value alone
- `tar1090_db`: an index at schema version 3 is not rebuilt; one at version 2 is
- adsbdb: **PIA set → no HTTP call of either kind, no cache file, no log line**
- adsbdb: **LADD set → aircraft call made, route call not made, one `suppressed`
  log line**
- adsbdb: military set → both calls made, nothing skipped
- adsbdb: `db_flags is None` → both calls made
- adsbdb: `db_flags = 0` → both calls made
- adsbdb: LADD **and** PIA both set → PIA wins, nothing called

The LADD asymmetry test is the one that matters. It is the change most likely to be
"simplified" later into skipping both calls, and the test is what stops that.

---

## Explicitly out of scope

- **`flag_filter`.** A military or "interesting" panel is panel-design work and
  needs this field to exist first.
- **Decoded flag properties on the schema.** One consumer, integer is enough.
- **Reclassifying HTTP 400 as permanent rather than transient.** A real finding from
  the last brief and worth its own look, but unrelated to flags and not to be bundled
  in.
- **Display of flags.** Nothing on the wall changes.
- **Backfilling `db_flags` into stored records.** Acquired on next observation, as
  with `category`.
