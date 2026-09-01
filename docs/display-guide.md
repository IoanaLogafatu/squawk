# Display Modules — Developer Guide

A **display** is a module that consumes the aircraft list and writes it somewhere a human can see — a screen, a web page, a terminal, an LED strip. Mechanically, displays are not a separate type: they implement the same `BaseModule` interface as filters and enrichments and occupy the same kind of slot in the chain. They are documented separately because they are the most common thing a Squawk user will want to build for themselves.

This guide covers the conventions, three worked examples (`console`, `http`, `epaper`), and a checklist for writing your own.

## What makes a module a "display"

By convention, a display:

- Reads from the aircraft list and produces side effects — writes to a screen, sends to clients, draws to hardware.
- Returns its input list **unchanged**, so subsequent modules still see the same data.
- Lives near the end of the chain, after filters have reduced the list to what should actually be shown.
- Handles the empty list gracefully — "no aircraft" is a normal state, not an error.
- Handles `UNKNOWN` (`None`) fields gracefully — registration, type, distance and the rest are all individually optional.

None of this is enforced. A display that mutates the list or sits mid-chain will run; it just won't behave like the rest.

## Position in the pipeline

Displays come at the end of each processor chain. You can define multiple independent processor chains with different displays and filters:

```toml
[processors.screen]
enabled               = true
poll_interval_seconds = 5
modules               = ["tar1090_db", "adsbdb", "closest_filter"]
display               = "epaper"

[processors.pushover]
enabled               = true
poll_interval_seconds = 5
modules               = ["ground_distance_filter", "tar1090_db", "registration_filter", "adsbdb"]
display               = "pushover"
```


## Worked examples

### console — the minimal case

```python
class ConsoleDisplay(BaseModule):
    def process(self, aircraft: list[Aircraft]) -> list[Aircraft]:
        if not aircraft:
            print("  ○ no aircraft")
        else:
            a   = aircraft[0]
            reg = a.airframe.registration or "???"
            typ = a.airframe.type_description or a.airframe.type_code or "???"
            print(f"{reg}  {typ}")
        return aircraft
```

The full shape of a display in eight lines. Things to notice:

- Empty-list branch handled explicitly.
- `aircraft[0]` is the convention: assume the chain has already filtered to the target. If it hasn't, you'll show whichever aircraft happens to be first.
- `UNKNOWN` fields guarded with `or "???"`.
- Input list returned unchanged.

If you've never written a module before, start by copying this and swapping `print()` for your sink of choice.

### http — background server with shared state

The HTTP display serves a live web page that auto-updates as new aircraft arrive. `process()` is still trivial; the real work happens in the constructor.

```python
class HttpDisplay(BaseModule):
    def __init__(self, cfg: dict) -> None:
        port            = int(cfg.get("port", 7700))
        self.chain_name = str(cfg.get("chain_name", "default"))
        panel_cfg       = (cfg.get("panels", {}) or {}).get(self.chain_name, {})
        self.panel_title = str(panel_cfg.get("title", self.chain_name.replace("_", " ").title()))
        self.slot        = int(panel_cfg.get("slot", 0))
        self.layout      = str(panel_cfg.get("layout", "card"))
        self.bands       = [str(b) for b in (panel_cfg.get("bands") or [])]
        self._state = SharedState()
        server      = ThreadingHTTPServer(("", port), make_handler(self._state))
        threading.Thread(target=server.serve_forever, daemon=True).start()

    def process(self, aircraft: list[Aircraft]) -> list[Aircraft]:
        self._state.update(self.chain_name, self.panel_title, self.slot, aircraft,
                           layout=self.layout, bands=self.bands)
        return aircraft
```

The pattern: anything that needs to live outside the `process()` call — a server, a worker thread, a hardware handle — is built in `__init__` and stashed on `self`. `process()` pokes the shared state and returns immediately. The pipeline never blocks on an HTTP client.

#### Panel configuration

Each processor chain that renders to `http` gets its own card on the dashboard. Panels are keyed by chain name — a chain named `[processors.low_level]` picks up its panel block from `[display.http.panels.low_level]`:

```toml
[display.http]
port = 7700

[display.http.panels.low_level]
title = "Approach & Low (< 5k ft)"
slot  = 1

[display.http.panels.mid_level]
title = "Mid Level (5k–15k ft)"
slot  = 2
```

The dashboard is a fixed 4×2 grid, matching the physical wall it is built for. `slot` names a position in it:

```
1  2  3  4
5  6  7  8
```

All eight slots always render. Slot 4 is top-right whether or not slots 1–3 are occupied, so a chain that dies leaves a gap where it was instead of shuffling the survivors into new positions — on a wall, positional memory is worth more than a tidy layout. A slot with no chain assigned shows its number and `UNASSIGNED`; that is a normal state, not an error. It is distinct from `NO TARGET`, which means a chain is running and currently sees nothing.

`slot` is **required**, and must be an integer from 1 to 8 that no other chain claims — a wrong or absent position is a visible defect on the wall, so the loader rejects it at startup. `title` is optional and falls back to a title-cased chain name (`low_level` → `Low Level`). The panel block itself is required too: a chain with `display = "http"` and no matching `[display.http.panels.<chain>]` block fails at startup rather than falling back silently, so a renamed chain can't orphan its panel unnoticed. A block still carrying the old `order` key is rejected with a message pointing at `slot`.

#### Panel layouts

A panel block's optional `layout` key selects which renderer draws it. It defaults to `"card"`, the single-aircraft view described above, so **every existing panel block keeps working untouched**.

```toml
[display.http.panels.panel_one]
title  = "Overhead"
slot   = 1
layout = "list"
bands  = ["D", "C", "B", "A"]
```

`layout = "list"` renders one fixed row per entry in `bands`, top to bottom exactly as written, and pairs with the `band_closest` selector upstream:

```toml
[processors.panel_one]
enabled               = true
poll_interval_seconds = 5
modules               = ["band_closest", "adsbdb"]
display               = "http"
```

Each row is four lines and nothing else:

```
FL320 Air France
F-GUOB Boeing 777-200
CDG Paris, France
→ORD Chicago, USA
```

| Line | Content |
|---|---|
| 1 | Flight level, airline (`route.airline_name`) |
| 2 | Ident, manufacturer + type |
| 3 | Origin IATA, municipality, country |
| 4 | `→` destination IATA, municipality, country |

The leading `→` on line 4 marks the destination without a label and left-aligns the two airport codes into a column the eye reads down; it sits hard against the left edge. Still deliberately absent: no callsign as a field of its own, no climb/descend arrow, no distance, no row heading. The space that buys goes into font size — this is read from across a room, and a field earns its place only against legibility.

The ident is the payload's existing `ident` key: registration → callsign → ICAO hex. Registration is a database lookup while the callsign is broadcast, so they fail independently — a PIA or `~` address has both lookups skipped by design yet still transmits a callsign, and on an aircraft's first cycles `adsbdb` has not run. Hex is a genuine last resort.

- **Rows are placed by band letter, not by list position.** `band_closest` returns between zero and N aircraft and gives no indication of which bands are missing, so the renderer matches each aircraft's `location.altitude_band` to a configured letter. An aircraft whose band no row claims is not rendered.
- **An empty band still occupies its row**, carrying no text. The rows hold their positions and the wall does not re-read itself each cycle. There are no headings to fall back on — which is also why `bands` takes no labels: **each row shows the aircraft's own flight level rather than a configured band caption**, so a row can never claim an altitude its occupant does not have.
- **The flight level is `FL` + hundreds of feet, zero-padded to three digits** — `29000` → `FL290`, `2000` → `FL020`. Applied at every altitude, including below the transition altitude where it is not strictly what a flight level means: fixed-width labels keep the rows aligned, and this is a wall display, not an ATC position.
- **The city, not the airport's full name.** Lines 3 and 4 use `route.origin_municipality` / `route.destination_municipality`, filled by `adsbdb` alongside the airport name. For CDG the name is *Charles de Gaulle International Airport* where the municipality is *Paris*, and an audience that wanted line 3 at all wants the city. The full name stays in `origin_name` / `destination_name` for anything else that wants it. A compound municipality — adsbdb occasionally answers `Cincinnati / Covington` — is split on the slash and the first city kept: one city read across a room beats two thirds of two.
- **Two country names are shortened** — `United Kingdom` → `UK`, `United States` (and `United States of America`) → `USA`. An explicit two-entry map, not a general abbreviation scheme.
- **The manufacturer is normalised to its brand name first.** adsbdb returns the registered legal entity — `Boeing Company`, `Airbus Sas`, `Atr - Gie Avions De Transport Regional` — which is both wrong to read and long enough to truncate the variant code that is the interesting part of the line. An explicit map, seeded from what is actually in the aircraft cache and matched case-insensitively, turns those into `Boeing`, `Airbus` and `ATR`; anything unmapped passes through untouched. It is a map rather than a rule that strips trailing corporate words, which would mangle any manufacturer whose brand legitimately contains one. This is a display transform: `airframe.manufacturer` keeps whatever the source gave it.

  The order matters. Normalising **before** the rule below means its prefix check compares `Boeing` against `BOEING 737-800` and strips correctly; normalising after would leave it comparing `Boeing Company`, missing, and concatenating instead.
- **Manufacturer and type are then joined without saying "Boeing" twice.** The two sources differ: `tar1090_db` writes `BOEING 737-800` with the manufacturer baked in and in caps, `adsbdb` overwrites with `737NG 8AS/W` and carries `manufacturer` separately, so prefixing blindly gives `Boeing BOEING 737-800`. In order: no description → the manufacturer alone; no manufacturer → the description unchanged; a description that starts with the manufacturer (compared case-insensitively) has that prefix replaced with the manufacturer's own casing, `BOEING 737-800` + `Boeing` → `Boeing 737-800`; otherwise the two are concatenated, `Boeing` + `737MAX 8 200` → `Boeing 737MAX 8 200`.

  The description-only case is the normal state for an aircraft's first cycle or two — `tar1090_db` answers before `adsbdb` does — so a row reading in capitals is expected, not a defect. **Known limitation:** where the two sources spell the manufacturer differently, `De Havilland Canada` against `DEHAVILLAND DHC-8`, the prefix match misses and the name doubles. Rare, and visible on the wall when it happens.
- **Every line collapses in place, and the row keeps its height** (`grid-auto-rows: 1fr`). No airline leaves line 1 as the flight level alone; no manufacturer or type leaves line 2 as the ident alone. **A missing route is words, not punctuation:** line 3 reads `Origin unknown` and line 4 `→Destination unknown`, and both lines are always drawn. A half-known route renders the resolved side normally and gives the other its unknown text; an airport with no municipality reads `CDG France`, and with no country either, `CDG` alone.

`layout` and `bands` are validated at startup: `layout` must be `"card"` or `"list"`, `"list"` requires a non-empty `bands`, `bands` with any other layout is an error naming both keys, and each entry must be a single letter `A`–`Z`, unique within the panel — two rows claiming `C` is the same class of mistake as two panels claiming a slot.

The letters are **not** checked against `[modules.altitude_band].edges`. The loader can see both, but checking would couple display config to module config. A `bands` entry of `F` in a four-band installation renders a permanently empty row — visible on the wall, and self-diagnosing.

Notable details:

- `daemon=True` so the server thread dies when the pipeline shuts down.
- `SharedState` is a small thread-safe pub/sub between the module thread (writer) and request handler threads (readers). HTTP clients are not the module's problem; they read from the shared state independently.
- The payload sends the full aircraft list per panel — an empty chain is an empty list, not `null`.
- Each panel carries `updated_epoch` (unix seconds). The page renders the age beside the callsign chip and dims the card once it stops advancing, so one hung chain among eight is visible.
- The payload's top-level `system` key carries `system.snapshot()`; the header reads `tracked` from it to show `MONITORING n FLIGHTS`, and shows `MONITORING — FLIGHTS` before the first ingest cycle has published anything.
- The link dot in the header is driven by `EventSource` state — green `LINK OK`, amber `RECONNECTING`, red `LINK DOWN` — with a text label beside it, because colour alone is a poor signal across a room. Only the healthy state animates.

### epaper — hardware, with throttling

E-paper screens have constraints the other examples don't: a full refresh takes ~2 seconds, panels wear with repeated writes, and the SPI bus doesn't tolerate concurrent access. The module interface is unchanged, but the implementation has to respect the hardware.

The pattern for any slow or wear-sensitive sink:

- **De-duplicate.** Keep `self._last_rendered` and compare. If the new value matches the last, return without touching the screen.
- **Throttle.** Track `self._last_draw_time`. Enforce a minimum interval between writes regardless of how often `process()` is called.
- **Long-lived handle.** Initialise the e-paper driver in `__init__` and reuse it. Opening the SPI bus per cycle is both slow and a good way to deadlock against your own previous call.

From outside, the module still looks like `process(aircraft) -> aircraft`. The complexity is internal — exactly as it should be.

Hardware caveat for systemd on the Pi: the service user must be in the `spi`, `gpio`, and `i2c` groups, or the module will work interactively but fail when run as a service.

## Writing your own — checklist

1. **Subclass `BaseModule`** and implement `process(aircraft) -> aircraft`.
2. **Add the factory:** `def get(cfg: dict, ctx: ModuleContext) -> YourDisplay`.
2a. **Use `ctx`, not global config.** `ctx.module_dir` (`<data_dir>/display/<name>`) is
    where your display writes state or cache files — create it on first write, same as any
    module. `ctx.observer` is the receiver position. There used to be an undocumented
    `data_dir` config key that existed only so tests could override the path; it is gone —
    tests now build a `ModuleContext` pointed at `tmp_path` instead.
3. **Always return the list.** Even when you've done nothing else. Returning `None` breaks every module after you.
4. **Handle the empty list.** It will happen, often. Decide what your display shows when there's nothing overhead.
5. **Guard `UNKNOWN` fields.** Every field on every aircraft can be `None`. Decide your fallback (`"—"`, `"???"`, omitting the row).
6. **Long-lived resources in `__init__`.** Servers, hardware handles, open files, threads — build once, reuse.
7. **Slow output stays out of `process()`.** Anything that waits on a client, screen, or network call should run on a background thread reading from shared state. The processor doesn't know your output is expensive.
8. **De-duplicate and throttle if writes are costly.** Hardware especially.
9. **Don't raise.** A display crash should not take the pipeline down. Wrap risky I/O in `try/except` and log.

## Testing

Display modules are easy to test because `process()` is a pure function with side effects:

- Construct the module with a config dict.
- Hand it a list of `Aircraft` objects (real or built in-test).
- Assert on the side effect (captured stdout, mock screen calls, HTTP fetch against the bound port).
- Hand it `[]` and assert the empty branch behaves.
- Hand it aircraft with various `UNKNOWN` fields and assert nothing crashes.

For hardware-backed displays, mock the driver at construction time — the module should accept a driver object via cfg (or via dependency injection in tests) rather than importing it at module level.

## Common pitfalls

- **Reading more than `aircraft[0]`** without making your assumption explicit. If you want to render the whole list, fine — but document it, because the next person to add a filter upstream will be surprised when their "single closest" result gets replaced by a scrolling list.
- **Forgetting to return.** The single most common bug. Always `return aircraft` at the bottom of `process()`.
- **Blocking in `process()`.** Anything that waits — a network client, a slow disk, a screen refresh — holds up the next ingestor cycle. Push it to a background thread.
- **Treating `None` as an error.** `UNKNOWN` is normal. Plan for it in every field you render.
- **Re-opening hardware per cycle.** Always a bug. Build the handle once, keep it on `self`.
