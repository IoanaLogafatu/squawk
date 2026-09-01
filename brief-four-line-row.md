# Brief: four-line row for the `list` layout

## Goal

Replace the two-line row in the HTTP `list` layout with a four-line row. The
layout, config and band-placement logic are unchanged — this is a rewrite of
what one row draws, plus one new field in the payload.

Driven by two panels of two bands each rather than one panel of four, so each
row now has four lines' worth of height to work with.

---

## The row

```
FL320 Air France
F-GUOB Boeing 777-200
CDG Paris, France
→ORD Chicago, USA
```

| Line | Content |
|---|---|
| 1 | Flight level, airline |
| 2 | Registration, manufacturer + type |
| 3 | Origin IATA, origin municipality, origin country |
| 4 | `→` destination IATA, destination municipality, destination country |

The leading `→` on line 4 marks the destination without a label and left-aligns
the two airport codes into a column the eye reads down. Keep it hard against the
left edge.

Still deliberately absent: no callsign as a field of its own, no climb/descend
arrow, no distance.

### Field sources

| Shown | Field |
|---|---|
| `FL320` | derived from `location.altitude_feet` |
| `Air France` | `route.airline_name` |
| `F-GUOB` | the existing `ident` key (registration → callsign → hex) |
| `Boeing 777-200` | `airframe.manufacturer` + `airframe.type_description`, see below |
| `CDG` / `ORD` | `route.origin_iata` / `route.destination_iata` |
| `Paris` / `Chicago` | **new** — see Payload |
| `France` / `USA` | `route.origin_country` / `route.destination_country` |

---

## Payload change

`render_aircraft_dict` currently maps only the airport `name` from adsbdb's
route block. The row needs the municipality: adsbdb returns both
(`"name": "Reus Airport"`, `"municipality": "Reus"`), and for CDG the name is
*Charles de Gaulle International Airport* where the municipality is *Paris*.
The city is what an audience that needed line 3 at all actually wants.

Check whether `modules/adsbdb.py` `_apply` currently stores `municipality`. If
the schema has no field for it, add `origin_municipality` and
`destination_municipality` to `AircraftRoute` alongside the existing
`origin_name` / `destination_name`, populate them in `_apply`, and expose them
in `render_aircraft_dict`. Do not repurpose `origin_name` — the airport's full
name is legitimate data and something else may want it.

Existing cached route files under `<data_dir>/modules/adsbdb/route/` hold the
full API response, so the municipality is already on disk and will populate on
the next read without a cache flush. Confirm this rather than assuming it.

### Country abbreviation

`render_aircraft_dict` already shortens `United Kingdom` → `UK`. Extend the same
treatment to `United States of America` → `USA`. Keep it as the existing
explicit map — two entries, not a general abbreviation scheme.

---

## Manufacturer rule

`type_description` comes from two sources with different conventions:
`tar1090_db` writes `BOEING 737-800` (manufacturer included, all caps), adsbdb
overwrites with `A320 214`, `737NG 8AS/W`, `737MAX 8 200` (no manufacturer).
Prefixing `manufacturer` blindly gives `Boeing BOEING 737-800`.

In order:

1. No `type_description` → show `manufacturer` alone.
2. No `manufacturer` → show `type_description` unchanged.
3. `type_description` starts with `manufacturer`, compared case-insensitively →
   replace that prefix with the manufacturer's own casing.
   `BOEING 737-800` + `Boeing` → `Boeing 737-800`.
4. Otherwise → `{manufacturer} {type_description}`.
   `Boeing` + `737MAX 8 200` → `Boeing 737MAX 8 200`.

Rule 3 is the one that matters; the rest are the edges around it.

Known limitation, not to be fixed here: when the two sources spell the
manufacturer differently — `De Havilland Canada` against a description reading
`DEHAVILLAND DHC-8` — rule 3 misses and rule 4 doubles the name. Visible on the
wall when it happens, and rare.

Note rule 2 is the normal state for the first cycle or two: `tar1090_db` supplies
a description with no manufacturer, so the row reads in capitals until adsbdb
resolves and rules 3 or 4 take over. Expected, not a defect.

---

## Missing fields

Every line collapses in place. The row keeps its height either way —
`grid-auto-rows: 1fr` is doing that job and must not be undone.

- No `airline_name` → line 1 is the flight level alone.
- No manufacturer or type → line 2 is the ident alone.
- **No route → line 3 reads `Origin unknown`, line 4 reads `→Destination unknown`.**
  This replaces the old single-line `??? → ???`, which does not survive the
  split: `???` / `→???` reads as broken rather than unknown. Both lines are
  always drawn.
- Partial route → the resolved side renders normally, the unresolved side takes
  its `unknown` text.
- Origin resolved but no municipality → `CDG France`. No country either →
  `CDG` alone.

---

## Tests

Extend `tests/test_http_display.py`:

1. Municipality appears in the payload for both origin and destination, `None`
   passing through unchanged.
2. `United States of America` → `USA`; the existing UK case still passes.
3. Manufacturer rule, one test per branch, including the `BOEING 737-800` +
   `Boeing` prefix case and the `737MAX 8 200` concatenation case.
4. Manufacturer present, description absent → manufacturer alone.
5. Description present, manufacturer absent → description unchanged, caps intact.

If `AircraftRoute` gains fields, extend `tests/test_module_adsbdb.py`:

6. `_apply` populates municipality from a full route fragment.
7. Storage round-trip preserves the new fields.

---

## Docs

`docs/display-guide.md` — update the `list` layout section for the four-line
row, the municipality preference over airport name, the country abbreviations,
and the manufacturer rule with its known limitation. The two-line row is gone,
not an option.

`docs/modules-reference.md` — if `AircraftRoute` gained fields, note them under
the `adsbdb` entry.
