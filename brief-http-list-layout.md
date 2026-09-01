# Brief: `list` layout for the HTTP display

## Goal

A second panel layout that renders one aircraft per altitude band as fixed rows,
turning `band_closest`'s output into panel 1.

The existing single-aircraft layout stays as the default and is unchanged.

---

## The row

Two lines, nothing else:

```
FL290 Ryanair 737NG 8AS/W
9H-QCR AGP → LPL
```

Line 1 — flight level, airline, aircraft type.
Line 2 — registration, origin → destination.

Deliberately absent: no callsign, no climb/descend arrow, no distance, no row
heading. The panel is four of these stacked, high band at the top. Space saved
here goes into font size — this is for a TV wall read from across a room, and
every field that earns its place must earn it against legibility.

### Field sources

| Shown | Field |
|---|---|
| `FL290` | derived from `location.altitude_feet` |
| `Ryanair` | `route.airline_name` |
| `737NG 8AS/W` | `airframe.type_description` |
| `9H-QCR` | `airframe.registration` |
| `AGP → LPL` | `route.origin_iata` / `route.destination_iata` |

### Flight level

`FL` + `round(altitude_feet / 100)`, zero-padded to three digits: 29000 → `FL290`,
2000 → `FL020`, 0 → `FL000`.

Applied at every altitude including below the transition altitude, where it is
not strictly what a flight level means. That is a deliberate choice: fixed-width
labels keep the four rows aligned, and this is a wall display, not an ATC
position.

### Missing fields

Each line collapses gracefully rather than reserving space:

- No `airline_name` (GA, most bizjets) → `FL020 CESSNA 172`.
- No `type_description` → `FL020` alone.
- No registration → fall back to `route.callsign`, then to `meta.icao_hex`.

  The two fail independently: registration is a hex lookup in `tar1090_db` or
  `adsbdb`, while the callsign is broadcast by the aircraft and needs no
  database at all. PIA and `~` addresses have both lookups skipped by design yet
  still transmit a callsign, and on an aircraft's first cycles `adsbdb` has not
  yet run. Callsign is also the more useful of the two on screen — it is a
  first-class search term on Flightradar24, where the hex is displayed but not
  searchable. Hex is a genuine last resort.

  Note this is the one place a callsign appears; it is a fallback identifier,
  not a field of its own.
- **No route → `9H-QCR ??? → ???`.** This is the common case, not the edge
  case: adsbdb has no route for most `SHT`, `EZY` and `RYR` callsigns. The
  placeholder holds the line's shape so four rows stay visually even.

Partial route uses the same marker per side: `AGP → ???`.

---

## Placement, and why the band letter matters

`band_closest` returns between zero and N aircraft with no indication of which
bands are missing, so row position **cannot** be inferred from list order. Each
aircraft is placed by reading `location.altitude_band` and matching it to a
configured row.

A band with no aircraft renders as an empty row that still occupies its
height — the four rows hold their positions and the wall does not re-read itself
each cycle. Empty rows carry no text; there are no headings to fall back on.

---

## Config

```toml
[display.http.panels.panel_one]
title  = "Overhead"
slot   = 1
layout = "list"
bands  = ["D", "C", "B", "A"]
```

`bands` is an ordered list of band letters, rendered top to bottom as written.
No labels — the flight level on each row identifies its own band, and an empty
row needs no caption.

`layout` is optional, defaults to `"card"`, so every existing panel block keeps
working untouched.

### Validation

Extend `_check_http_panels` in `config.py`:

- `layout`, if present, must be `"card"` or `"list"`.
- `layout = "list"` requires a non-empty `bands` list.
- `bands` present with any other layout is an error naming both keys.
- Each entry is a single character, `A`–`Z`.
- Entries are unique within a panel — two rows claiming `C` is the same class
  of mistake as two panels claiming a slot and should read the same way.

**Do not validate the letters against `[modules.altitude_band].edges`.** The
loader can see both, but checking would couple display config to module config,
which is the coupling this architecture has spent several sessions removing. A
`bands` entry of `F` in a four-band installation renders a permanently empty
row — visible on the wall, and self-diagnosing.

---

## Payload

`render_aircraft_dict` in `display/http/server.py` omits the band. Add:

```python
"altitude_band": a.location.altitude_band,
```

`SharedState.update` must carry `layout` and `bands` through to the browser.
`HttpDisplay.__init__` already reads `panel_cfg`; read them there and pass them
into `update` alongside `slot` and `title`.

The existing keys stay as they are — the card layout still uses `distance`,
`vrate` and the rest, and this brief adds a layout rather than replacing one.

---

## Client

New `renderListCard(panel)` in the page JavaScript, selected in `renderPanels`
when `panel.layout === 'list'`; everything else continues to `renderCard`.

For each letter in `panel.bands`, in order: find the aircraft whose
`altitude_band` matches (at most one — `band_closest` guarantees it), render the
two lines, or render an empty row of the same height.

An aircraft whose band matches no configured letter is not rendered.

Panel header, badge, age indicator and stale handling are panel-level and stay
exactly as they are. Follow the existing CSS idiom in the page; row typography
should scale up to fill the space freed by dropping distance and the vertical
rate arrow.

---

## Tests

Extend `tests/test_http_display.py`:

1. `render_aircraft_dict` includes `altitude_band`, passing `None` through
   unchanged.
2. A `list` panel's payload carries `layout` and `bands`.
3. A `card` panel's payload is unchanged — regression guard on the default path.
4. `SharedState.update` on a list panel holding aircraft in bands D and B only
   produces a payload whose entries carry their letters.

Extend `tests/test_config.py`:

5. Valid `list` panel with four bands passes.
6. `layout = "list"` with no `bands` fails, naming the panel.
7. `bands` present with `layout = "card"` fails, naming both keys.
8. Duplicate letter within one panel fails.
9. Invalid entry (`"AB"`, `"1"`, lowercase, empty string) fails.
10. `layout = "grid"` fails, listing the valid values.
11. Existing panel blocks with neither key still validate.

---

## Wiring — example file only

In `config.toml.example`, replace the four altitude chains with one:

```toml
[processors.panel_one]
enabled               = true
poll_interval_seconds = 5
modules               = ["band_closest", "adsbdb"]
display               = "http"
```

Remove `[processors.low_level]`, `[processors.mid_level]`,
`[processors.upper_level]` and `[processors.high_cruise]`, their
`[display.http.panels.*]` blocks, and the `[modules.low_altitude]`,
`[modules.mid_altitude]`, `[modules.upper_altitude]` and
`[modules.high_altitude]` blocks. Add the empty `[modules.band_closest]` block,
now referenced by a chain. `panel_one` takes slot 1; slots 2–4 render as
UNASSIGNED, which is correct — they are waiting for V2 content.

**`config.toml` is gitignored and is not in scope for this brief.** It is edited
by hand and does not resemble the example.

---

## Docs

`docs/display-guide.md` — document `layout` and `bands`, that `card` is the
default, that rows are placed by band letter rather than list position, that an
empty band still occupies its row, and that the row shows the aircraft's own
flight level rather than a configured band caption.
