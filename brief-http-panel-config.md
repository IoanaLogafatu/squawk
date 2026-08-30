# Brief: per-panel config for the http display

**Scope:** `display/http/__init__.py`, `display/http/server.py`, `processor/processor.py`,
`config.py`, `config.toml`, `config.toml.example`, `docs/display-guide.md`, and `tests/`.

**Do not** touch `modules/`, `storage/`, `ingestor/`, or any other display.

> **Deploy note:** code and `config.toml` must ship together. `panel` and `panel_title`
> are being removed from the processor config and replaced by panel blocks under the
> http display.

---

## Problem

Two things are wrong with how panels are configured today.

`ProcessorConfig` carries `panel` and `panel_title`. These are the http display's layout
model promoted into the core config schema, and `processor.py` injects them into *every*
display's config dict. Console, epaper and pushover receive them and ignore them.

Panel order is a race. `SharedState._panels` is a plain dict and a panel is inserted on
its chain's first `process()` call, not at config load. Order therefore depends on which
processor thread finishes first, and the 4x2 grid reshuffles between restarts. The live
config groups chains as altitude bands then special targets; that grouping is not
currently honoured on screen.

There is also dead state to remove while in the same file — see Change 4.

---

## Change 1 — panel blocks in the http display config

Panels are keyed by **chain name**. A chain named `[processors.low_level]` is matched to
`[display.http.panels.low_level]`. No explicit key links them.

```toml
[display.http]
port = 7700

[display.http.panels.low_level]
title = "Approach & Low (< 5k ft)"
order = 1

[display.http.panels.mid_level]
title = "Mid Level (5k–15k ft)"
order = 2

# ... and so on for upper_level, high_cruise, closest_overhead,
#     watchlist, descending, climbing
```

Set `order` to match the existing grouping in `config.toml`: altitude bands 1–4
(low, mid, upper, high), then special targets 5–8 (closest_overhead, watchlist,
descending, climbing).

**Missing panel block is not an error.** Fall back to the chain name title-cased
(`low_level` → `Low Level`) and an order of 999 so unconfigured panels sort to the end.
Print one warning line at startup naming the chain, so the omission is visible:

```
  http display: no panel config for chain 'low_level' — using defaults
```

Do not add a distinct visual state for unconfigured panels. The startup warning plus an
obviously-wrong title on screen is sufficient.

---

## Change 2 — `chain_name` replaces `panel` and `panel_title`

In `config.py`, remove the `panel` and `panel_title` fields from `ProcessorConfig` and
remove them from both branches of `_load_processors()`.

In `processor/processor.py`, replace the two injected keys with one:

```python
display_cfg = dict(config.display.get(cfg.display, {}))
display_cfg["chain_name"] = cfg.name
display = get_display(cfg.display, display_cfg) if cfg.display else None
```

`chain_name` is generic — any display may legitimately want to know which chain it
serves — so this is not the same leak the `panel` keys were.

In `display/http/__init__.py`, `HttpDisplay.__init__` resolves its own panel config:

```python
self.chain_name = str(cfg.get("chain_name", "default"))
panels = cfg.get("panels", {}) or {}
panel_cfg = panels.get(self.chain_name)
if panel_cfg is None:
    print(f"  http display: no panel config for chain {self.chain_name!r} — using defaults")
    panel_cfg = {}
self.panel_title = str(panel_cfg.get("title", self.chain_name.replace("_", " ").title()))
self.panel_order = int(panel_cfg.get("order", 999))
```

The whole `[display.http]` table including `panels` is already passed through by the
processor, so no extra plumbing is needed.

---

## Change 3 — `SharedState` keeps the list, and carries order

In `display/http/server.py`, `SharedState.update()` currently discards everything but the
first aircraft. Change its signature and payload:

```python
def update(self, panel_id: str, panel_title: str, panel_order: int,
           aircraft: list[Aircraft]) -> None:
    with self._lock:
        self._panels[panel_id] = {
            "panel_id":   panel_id,
            "title":      panel_title,
            "order":      panel_order,
            "aircraft":   [render_aircraft_dict(a) for a in aircraft],
            "count":      len(aircraft),
            "updated_at": datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
        }
        payload = self._get_payload()
    # ... subscriber fan-out unchanged
```

`aircraft` becomes a **list** of rendered dicts rather than a single dict or `None`. An
empty chain yields `[]`.

`HttpDisplay.process()` passes `self.panel_order` through alongside the title.

This is groundwork for list and count layouts later. **Do not add a `layout` key or any
alternative renderer in this change** — only the single-aircraft card exists, and a
config key with one valid value is surface without behaviour.

---

## Change 4 — remove the dead single-aircraft path

Still in `display/http/server.py`:

- Delete `self._single_aircraft`, its assignment in `update()`, and the `"single"` key
  from `_get_payload()`. It is overwritten by whichever of the eight chains reported
  last, so it is meaningless with multiple panels.
- Delete `render_data()`. It is referenced only by its own tests.
- In the page JavaScript, delete the `if (state.single)` fallback branch in
  `renderState()` and the `grid-1` class it sets. With no panels, render the existing
  empty-state markup.

Remove the corresponding `render_data` tests from `tests/test_http_display.py`. The
behaviour they cover (ident falling back registration → callsign → hex) is already
covered via `render_aircraft_dict`; if any case is not, keep it by testing
`render_aircraft_dict` directly instead.

---

## Change 5 — render in order, read from the list

In the page JavaScript, `renderState()` currently iterates `Object.keys(panels)`. Sort by
the new `order` field, falling back to the panel key for stable ordering when two panels
share an order:

```javascript
const panelKeys = Object.keys(panels).sort((a, b) => {
  const d = (panels[a].order ?? 999) - (panels[b].order ?? 999);
  return d !== 0 ? d : a.localeCompare(b);
});
```

`renderCard()` must now read `panel.aircraft[0]` rather than `panel.aircraft`, treating
an empty array the same as the current `null` (the "scanning for traffic" state).

---

## Change 6 — config and docs

In `config.toml` and `config.toml.example`:

- Remove `panel` and `panel_title` from all nine processor blocks.
- Add the eight `[display.http.panels.*]` blocks with the titles currently held in
  `panel_title`, and `order` per Change 1.
- The pushover chain has no panel block and needs none — it does not use the http
  display.
- Keep placeholder credentials and generic coordinates in `config.toml.example`.

In `docs/display-guide.md`, document the panel blocks: keyed by chain name, `title` and
`order` both optional, missing block falls back to a title-cased chain name at the end of
the grid.

---

## Explicitly out of scope

- Any new layout, renderer, or `layout` config key.
- Any new filter module.
- The header chip wrapping/collision issue in the panel cards.
- Any change to the grid CSS classes.
- Any change to `console`, `epaper` or `pushover` displays.

---

## Tests

Update `tests/test_http_display.py`:

1. **Existing multi-panel test** — `test_http_display_multi_panel_updates` constructs
   `HttpDisplay({"port": port, "panel_id": ..., "panel_title": ...})`. Rewrite to pass
   `chain_name` and a `panels` dict, and update the payload assertions to
   `data["panels"]["low_level"]["aircraft"][0]["ident"]`.

2. **Title and order from panel config** — a chain with a matching panel block picks up
   its configured title and order.

3. **Missing panel block falls back** — a chain with no matching block gets the
   title-cased chain name and order 999, and does not raise.

4. **Panel payload carries the full list** — a chain returning three aircraft produces a
   three-element `aircraft` array and `count` of 3.

5. **Empty chain** — produces `aircraft: []` and `count: 0`, not `null`.

Add to `tests/test_config.py`:

6. **`ProcessorConfig` no longer has panel fields** — loading a config that still
   contains `panel`/`panel_title` keys ignores them without raising, so an old config
   file does not crash the loader.

---

## Verification

- `./runtests.sh` passes.
- Deploy code and `config.toml` together, restart, and confirm the eight panels render in
  the configured order: altitude bands on the top row, special targets on the bottom.
- Restart twice more and confirm the order is now stable rather than reshuffling.
- Temporarily remove one panel block and confirm the startup warning prints, that panel
  appears last with a title-cased name, and nothing crashes.
