# Brief: `altitude_band` enrichment module

## Goal

Tag every aircraft with a flight-level band letter at ingest, so downstream
filters and selectors can group by band without each one carrying its own
altitude thresholds.

The band letters are identity. Any human-readable form ("FL200–FL300") is
display copy and belongs in panel config, not in the schema and not in this
module.

---

## Schema change

Add to `AircraftLocation` in `schemas/aircraft.py`, directly after
`altitude_feet` — it is derived from it:

```python
altitude_band: Optional[str] = UNKNOWN   # Flight-level band letter, e.g. "C"
```

Update the class docstring to say the band letter is assigned by the
`altitude_band` module and is meaningless without the installation's
configured edges.

Check `schemas/encoder.py` and the storage reconstruction in
`schemas/aircraft.py` (around line 213). The new field must serialise and
deserialise, or it will be silently dropped on the way back out of storage.

---

## Module

New file `modules/altitude_band.py`.

Reads `a.location.altitude_feet` and nothing else. That field is already
normalised barometric altitude with `"ground"` mapped to `0`, which is exactly
what a flight level is measured against — so there is no conversion here and no
`alt_geom` fallback. A band derived from geometric altitude would not be a
flight level. If `altitude_feet` is `UNKNOWN`, `altitude_band` stays `UNKNOWN`.

Assignment is half-open upward. With `edges = [10000, 20000, 30000]`:

| Band | Condition | Reads as |
|---|---|---|
| A | `alt < 10000` | below FL100 |
| B | `10000 <= alt < 20000` | FL100–FL200 |
| C | `20000 <= alt < 30000` | FL200–FL300 |
| D | `alt >= 30000` | above FL300 |

Letters are generated from the edge count: N edges give N+1 bands, lettered
from `A`. Do not hard-code four.

---

## Config

```toml
[modules.altitude_band]
edges = [10000, 20000, 30000]
```

```python
KEYS = {"type", "edges"}
```

Validation, all raising `ValueError` at construction:

- `edges` is required — no default. An installation that doesn't say where its
  bands are doesn't get bands.
- Non-empty list of numbers.
- Strictly ascending.
- Each a positive multiple of 100, so every boundary is a real flight level and
  no derived label can come out as FL125.5.
- At most 25 edges, so the letters stay single characters.

---

## Wiring

Add `"altitude_band"` to `modules = [...]` on **both** `[ingestors.personal_adsb]`
and `[ingestors.concorde]` in `config.toml.example`. Concorde currently has no
`modules` key at all — add one containing only `altitude_band`.

**Do not add `tar1090_db` to Concorde.** Its absence there is deliberate, not an
oversight: the Concorde ingestor fabricates a complete airframe (`G-BOAC`,
operator, type code, type description, category), so a lookup would miss and
write nothing. `tar1090_db` is source-dependent — it fills gaps a particular
source leaves. `altitude_band` is source-independent: it is a pure function of
a field every source provides, so no ingestor legitimately opts out.

Ordering within the ingest module list does not matter. This module depends only
on `altitude_feet`, which the converter sets, not on anything `tar1090_db` adds.

---

## Note for the reviewer

Unlike `tar1090_db`, this writes *derived* state into storage:
`data/tracked_aircraft/*.json` will carry a band letter recomputed from
`altitude_feet` on every ingest cycle. It cannot go stale relative to the
altitude beside it, because both are rewritten together. Flagged because a
persisted derived field looks like a smell until you see that.

---

## Tests

New `tests/test_altitude_band.py`:

1. Each band assigned correctly for a mid-band altitude.
2. Boundary values — 10000 → B, 20000 → C, 30000 → D. This is the whole reason
   the module exists rather than four `altitude_filter` blocks.
3. `altitude_feet = 0` (ground) → A.
4. `altitude_feet = None` → band stays `UNKNOWN`, no exception raised.
5. Single edge → two bands, A and B.
6. Rejects: missing `edges`, empty list, descending order, duplicate values,
   non-multiple of 100, negative value, 26 edges.
7. Storage round-trip preserves the band.
8. Factory pooling — two references to `[modules.altitude_band]` yield one
   instance.

---

## Docs

`docs/modules-reference.md` — new entry under the enrichment modules, alongside
`tar1090_db`. State that it runs at ingest, that the letters are identity while
FL strings are display copy, and that changing `edges` changes the meaning of
every letter across the installation.

---

## Testing note

The Concorde ingestor is hard-coded to 5,000 ft, so with it enabled band A is
permanently occupied. Disable Concorde when testing anything that depends on a
band being empty.

---

## Next

Once this is in and live aircraft are carrying letters: `band_closest` — group
by `location.altitude_band`, take the minimum `distance_nm` in each, emit
ordered high to low.
