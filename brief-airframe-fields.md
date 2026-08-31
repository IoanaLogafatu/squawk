# Brief: three airframe facts at ingest — category, type code, type description

**Scope:** `schemas/aircraft.py`, `ingestor/personal_adsb/converter.py`,
`modules/tar1090_db.py`, `modules/adsbdb.py`, `display/http/server.py`,
`docs/readsb_aircraft_fields.md`, `docs/modules-reference.md`,
`tests/test_schema_aircraft.py`, `tests/test_personal_adsb.py`,
`tests/test_tar1090_db.py`, `tests/test_module_adsbdb.py`,
`tests/test_http_display.py`.

**Do not** change `storage/`, `processor/`, `config.py`, `main.py`, or any filter
module. No new filter module — this brief only makes the data available.

---

## Problem

**One field, three writers, three formats.** `airframe.aircraft_type` is written by
the converter from readsb's `desc` ("AIRBUS A-320"), by `tar1090_db` from the CSV
(guarded on `is None`), and by `adsbdb` from its own string ("737MAX 8 200"),
unguarded. What ends up in the field depends on which sources happened to answer.
Live panels show "AIRBUS A-321neo" on one card and "Challenger 850" on another —
same field, different shapes.

The cosmetic inconsistency is the lesser problem. The ICAO type designator is a
machine-readable identifier; the descriptions are prose that varies by source. The
current arrangement lets prose overwrite the designator, and the designator is
exactly what a type filter needs — matching "is this a 737" against `B38M` is a
prefix test, against free text it is guesswork.

`tar1090_db` makes this vivid. It reads both CSV columns and discards one:

```python
desc = (row[4].strip() if len(row) > 4 else "") or row[2].strip() or None
```

`row[2]` is the type code, `row[4]` the description. The result is assigned to a
local named `type_code` that holds a description whenever one exists. Both facts are
already being read off disk; one is thrown away.

**Aircraft class is not captured at all.** readsb emits ICAO ADS-B emitter category
as `category` — "A3" for the A320, "A5" for the 787-10, "A2" for a Challenger 850,
"B1" for a glider. It is in the transmission, costs nothing, needs no API, and is
absent from the schema. A live snapshot showed roughly three-quarters of aircraft
reporting a usable value.

This matters because altitude is currently standing in for it. A chain filtering
above 25,000 ft to mean "airliner" admitted a VistaJet Challenger to the "closest
passenger" panel this morning. Category answers that question directly.

---

## Change 1 — schema

`Airframe` gains three fields and loses one:

```python
type_code:        Optional[str] = UNKNOWN   # ICAO designator, e.g. "A320", "B38M"
type_description: Optional[str] = UNKNOWN   # Human-readable, e.g. "AIRBUS A-320"
category:         Optional[str] = UNKNOWN   # ADS-B emitter category, e.g. "A3"
```

`aircraft_type` is removed. Do not keep it as an alias — a field with three writers
and an ambiguous format is what this brief exists to eliminate, and leaving it in
place means the ambiguity survives. Backward compatibility is not required.

Document the category values in the field comment or `docs/readsb_aircraft_fields.md`,
since they are not self-explanatory: A0 no information, A1 light (<15,500 lb), A2
small (15,500–75,000), A3 large (75,000–300,000), A4 B757, A5 heavy (>300,000), A6
high performance, A7 rotorcraft, B1 glider, B2 lighter-than-air, B4 ultralight, B6
UAV, B7 space vehicle, C0–C7 surface vehicles and obstacles.

Note in the docs that category is operator-configured and therefore occasionally
wrong, that A0 is common, and that Mode S–only and MLAT tracks will not have it.
Anything consuming it needs to handle absence as a normal case, not an error.

## Change 2 — converter

Three lines, all from fields already in the raw record:

```python
type_code        = raw.get("t",        UNKNOWN),
type_description = raw.get("desc",     UNKNOWN),
category         = raw.get("category", UNKNOWN),
```

`t` is the designator readsb already supplies — confirmed present in live records
alongside `desc`. This makes the converter the primary source for all three, with
the enrichers filling gaps rather than competing.

## Change 3 — `tar1090_db` stops collapsing two columns into one

The database build currently reduces `row[2]` and `row[4]` to a single value. Change
the in-memory map and the SQLite schema to carry `(registration, type_code,
description)` as three columns, and have the enricher fill `type_code` and
`type_description` independently, each still guarded on `is None`.

This changes the persisted database shape, so the existing file must be rebuilt.
There is presumably already a version or freshness check governing when the CSV is
re-downloaded and the index rebuilt — use it, or add a schema version if none
exists, so the stale database is discarded rather than read with the wrong column
count. Say in the handback which mechanism was used.

## Change 4 — `adsbdb` writes to the description only

The unguarded overwrite becomes correct rather than merely deliberate:

```python
if ac.get("type"):
    aircraft.airframe.type_description = ac["type"]
```

adsbdb's string is the better display value, and it can no longer destroy the
designator because it no longer writes to that field. Keep the overwrite unguarded
and add a one-line comment saying adsbdb's description is intentionally preferred
over the tar1090 one — it is the only unguarded write in `_apply`, and without the
comment it reads as an oversight.

`registration` and the rest are unchanged.

## Change 5 — display

`render_aircraft_dict` currently emits `"aircraft_type": a.airframe.aircraft_type or "—"`.
Emit all three instead — `type_code`, `type_description`, `category` — and let the
renderer choose. Two call sites in the page JS read `a.aircraft_type`; both should
take `type_description` first and fall back to `type_code`, which is the priority the
current code achieves by accident.

While in there: `const typeStr = ...` at the top of `renderCard` is assigned and
never used — line 704 recomputes the same expression inline. Delete the unused
local.

No visible change to the wall. This is plumbing so the panel work has fields to
choose from.

---

## Change 6 — tests

- schema: the three fields exist, default to `UNKNOWN`, and survive a JSON round trip
- converter: all three populate from a raw record carrying `t`, `desc` and `category`
- converter: each is `None` when its raw field is absent — Mode S records with no
  category are normal, not an error
- `tar1090_db`: `type_code` and `type_description` are filled independently from the
  two CSV columns, and a row with an empty description still yields a type code
- `tar1090_db`: neither field is overwritten when already set
- `adsbdb`: writes `type_description`, leaves `type_code` untouched even when set
- display: the payload carries all three; the renderer prefers the description and
  falls back to the code
- a fixture exercising the real precedence end to end — converter sets both from
  readsb, `tar1090_db` finds nothing new, adsbdb replaces the description only, and
  `type_code` survives all of it

`tests/fixtures/adsb1.json` and `adsb2.json` may need a `category` field adding to
some records. Check first — the real readsb payloads they were captured from
probably already carry it.

---

## Explicitly out of scope

- **The category filter itself.** This brief makes the field available; the filter
  is its own brief once the panel design settles which categories each panel wants.
- **Deciding what "passenger jet" means.** A3+A4+A5 excludes bizjets, A2+A3+A5
  includes them. A panel design question.
- **Backfilling category into stored records.** Existing aircraft on disk lack the
  field and will acquire it on their next observation. No migration.
- **Any use of `type_code` for filtering, grouping or history.** Later.
