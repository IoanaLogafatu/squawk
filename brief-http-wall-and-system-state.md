# Brief: static 4×2 wall, honest status indicators, and system state

**Scope:** `system.py` (new), `storage/__init__.py`, `storage/disk_drive.py`,
`config.py`, `display/http/__init__.py`, `display/http/server.py`,
`config.toml.example`, `docs/display-guide.md`, `docs/storage-guide.md`,
`tests/test_http_display.py`, `tests/test_config.py`, `tests/test_system.py` (new).

**Do not** change `modules/`, `processor/`, `ingestor/`, `schemas/`, `main.py`,
or any filter module.

This brief is about the frame the panels sit in, not what the panels show. Panel
layout renderers, per-panel content design, and anything to do with how many
aircraft a chain displays are explicitly a later brief.

---

## Problem

Three separate things, all in the same file, all about the display telling the
truth.

**1. The grid is dynamic when the hardware is not.** `renderState()` sets
`dashboard.className = grid-${Math.min(count, 8)}` and the CSS carries a ladder of
`.grid-2` … `.grid-8` rules. The wall is a fixed 4×2 array of Sharp panels. A grid
that reflows when a chain dies means the remaining panels move, which on a physical
wall is worse than a gap — you lose the positional memory that makes a wall readable
at a glance. `order` is currently a sort key, so a lone chain with `order = 6` renders
top-left rather than in slot 6.

Above eight panels it also breaks outright: `Math.min(count, 8)` clamps to `.grid-8`,
which fixes `grid-template-rows: repeat(2, 1fr)`, so a ninth panel overflows rather
than reflowing.

**2. The status indicator is decorative.** `<div class="radar-dot"></div>` has no id
and is never referenced from JS. It pulses green on a 2s CSS animation regardless of
state. There is no `es.onerror` handler at all, so when the Squawk process dies the
SSE connection drops, `onmessage` stops firing, and the page holds its last payload
indefinitely — with the dot still pulsing green. The one failure a wall display must
make obvious is the one it currently disguises.

Per-panel liveness has the same gap from the other direction. `SharedState.update()`
already computes `updated_at` and puts it in the panel dict, and nothing on the client
ever reads it. A single chain hanging while the other seven run is invisible.

**3. There is no system-level state.** The header shows a clock generated client-side
by `setInterval`. There is nowhere for the installation to publish a fact about itself
— how many aircraft are being tracked, when an ingestor last polled successfully, how
much adsbdb budget is left. The first of these is wanted now: a header reading
`MONITORING 145 FLIGHTS`, where 145 is the size of the pot after stale expiry.

---

## Design note: system state is published, not polled

The count is a push from storage, not an ambient global that consumers read at will.
`save_aircraft_array()` knows the pot size the moment it finishes expiring stale
records; it announces it. Displays render whatever was last announced.

This matters because of what it rules out. A **filter** that branches on system state
stops being reproducible from its config — the chain is no longer "the list of modules
named in `[processors.x]`", which is the property the last several briefs bought.
So the rule, and it goes in the module docstring:

> Displays read system state. Filters do not.

Nothing enforces this yet and nothing should be built to enforce it — there is one
consumer and one producer. It is written down so the next person has to decide to
break it rather than drift into it.

---

## Change 1 — `system.py`

New top-level module, alongside `config.py`. Small deliberately.

```python
"""
system.py

Installation-level facts, published by whichever component owns them and read
by displays at render time.

Displays read system state. Filters do not — a filter that branches on system
state is no longer reproducible from its config block, which is the property
the module architecture exists to protect.

Values are whatever the publisher last set. There is no history, no expiry and
no schema; a key that has never been published is simply absent.
"""
```

API — three functions and a lock. No class, no singleton ceremony:

- `set(key: str, value) -> None`
- `get(key: str, default=None)`
- `snapshot() -> dict` — a copy, safe to serialise
- `clear() -> None` — tests only, same shape as `clear_module_pool()`

A single module-level dict guarded by a single `threading.Lock`. Writers are
ingestor threads, readers are chain threads; both are brief.

Keys defined by this brief: **`tracked`** — integer, aircraft currently in storage.
Do not invent others speculatively.

## Change 2 — storage publishes `tracked`

`disk_drive.save_aircraft_array()` ends with `self._expire_stale()`. Immediately
after it, publish:

```python
system.set("tracked", len(self.list_aircraft_hex_ids()))
```

`list_aircraft_hex_ids()` already applies the `STALE_SECONDS` window and does not
deserialise, so this is a directory stat pass, not a parse. Order matters: after
expiry, so the number matches what a reader would actually get back.

This is a backend contract, not base-class behaviour — there is one backend and
building the enforcement machinery for a second one that does not exist would be
speculative. Document it in `docs/storage-guide.md` under the backend contract:
a backend publishes `tracked` after saving. If a second backend ever lands,
that is the moment to reconsider.

## Change 3 — panel slots replace panel order

`order` becomes `slot`: an explicit position in the 4×2 wall, numbered

```
1 2 3 4
5 6 7 8
```

Slot 4 renders top-right whether or not slots 1–3 are occupied.

**Config validation** (`_check_http_panels`, which already exists and already runs
per http chain). Each becomes a hard error, consistent with the rest of the loader:

- panel block missing `slot`
- `slot` not an integer in 1–8
- two chains claiming the same slot — name both chains and the slot in the message
- panel block still carrying `order` — tell the reader to rename it to `slot`.
  Not for compatibility: the missing-`slot` check above already catches this case.
  It exists purely to turn "you're missing a key" into "the key you want is the
  one you already wrote, under its new name", because `config.toml` is gitignored
  and so survives every code change that invalidates it.

**`HttpDisplay.__init__`** reads `slot` instead of `order` and passes it to
`update()`. While in there, delete the `if panel_cfg is None:` fallback branch and
its warning print — `_check_http_panels` has made it unreachable, and it currently
says the opposite of what the config layer enforces.

**`SharedState.update()`** takes `slot` in place of `panel_order` and stores it.

Two renames while in there, since the signature is changing anyway:

- Replace `updated_at` with `updated_epoch`. The formatted `"%H:%M:%S UTC"` string
  is not something the client can do arithmetic on, and Change 5 needs an age.
  Nothing reads `updated_at` today, so keeping both would just mean shipping a
  second representation of the same fact on every broadcast. Format on the client.
- `panel_id` becomes `chain_name`. `HttpDisplay` already calls it `chain_name`,
  the config block is keyed by chain name, and `_check_http_panels` reports it as
  a chain — `panel_id` is the odd one out, and now that slots exist it actively
  misleads, since the thing that identifies a *panel* is its slot. Four sites,
  all in `server.py`: the `update()` parameter, the dict key it writes, the stored
  value, and `renderCard`'s title fallback (`panel.panel_id || 'TRAFFIC'`).

**`title` stays optional.** `slot` becomes required because a wrong or absent
position is a visible defect on the wall; a missing title is not, and a title-cased
chain name is a reasonable thing to fall back to. This matches the loader's stated
posture — structure must be declared, tuning knobs with a sensible default may
default. `docs/display-guide.md` currently states that both `title` and `order` are
optional, which will be half wrong after this change; correct it to say `slot` is
required and `title` is not.

## Change 4 — static grid

`#dashboard` becomes a fixed `repeat(4, 1fr)` × `repeat(2, 1fr)` grid. Delete the
`.grid-2` … `.grid-8` ladder and the `dashboard.className` assignment that selects
between them. Keep the `.grid-8`-derived tightened padding as the single padding
rule, since eight panels is now the only case.

`renderState()` builds eight cards by slot, not by iterating the panels it received:

```js
for (let slot = 1; slot <= 8; slot++) {
  html += panelsBySlot[slot] ? renderCard(panelsBySlot[slot]) : renderEmptySlot(slot);
}
```

Drop the "no panels at all" special case — with eight slots always rendered, zero
chains is just eight empty slots, which is the correct picture.

**`renderEmptySlot(n)`** is a slot with no chain assigned. Distinct from a chain
that is running and currently sees nothing, which already renders `NO TARGET`.
Show the slot number and `UNASSIGNED`, in the dim text colour, no border accent.
Plain text — no icon, no animation.

**Remove `⎈` throughout.** It is U+2388 HELM SYMBOL, a ship's wheel, and it appears
in the connecting state, the empty state and the initial markup. Replace with plain
text in the existing mono face — `NO TARGET`, `UNASSIGNED`, `CONNECTING`. Do not
substitute another glyph or an SVG; text renders identically on every panel
regardless of what fonts the Sharp displays resolve.

## Change 5 — indicators that mean something

**The link dot** gets an id and is driven by `EventSource` state. `EventSource`
reconnects on its own, so the three states map directly:

- `es.onopen` → green, `LINK OK`
- `es.onerror` with `readyState === CONNECTING` → amber, `RECONNECTING`
- `es.onerror` with `readyState === CLOSED` → red, `LINK DOWN`

Set the initial state to `CONNECTING` in the markup, not `LINK OK` — the page
should not claim a connection it has not made yet. Drop the `pulse` animation on
anything except the green state, so movement means healthy rather than meaning
nothing.

Put the text label next to the dot. A colour alone is a poor signal on a wall
seen from across a room, and it is useless to anyone reading the page who cannot
distinguish red from green.

**Per-panel age.** `renderCard` shows the panel's own last update time in the header
next to the callsign chip, and the card dims when `updated_epoch` is older than
three poll intervals. The client does not know a chain's interval, so send it:
`HttpDisplay` has `cfg`, but the interval lives on `ProcessorConfig` — pass
`poll_interval_seconds` through the `display_cfg` dict that `processor.run()`
already builds and injects `chain_name` into. Same mechanism, one more key.

If that turns out to need changes outside this brief's scope, fall back to a flat
30-second threshold and say so in the handback rather than widening the scope.

**Header count.** `_get_payload()` gains a top-level `system` key holding
`system.snapshot()`. The header renders `MONITORING {n} FLIGHTS` from
`state.system.tracked`, and shows `MONITORING — FLIGHTS` when the key is absent
— which is the honest reading before the first ingest cycle completes.

Keep the existing channel count. It answers a different question (how many chains
are configured) from the flight count (how many aircraft exist).

---

## Change 6 — documentation

`docs/display-guide.md` — the panel configuration section: `slot` replaces `order`,
what the numbering means, that all eight render always, and that an unassigned slot
is a normal state rather than an error.

`docs/storage-guide.md` — backend contract gains publishing `tracked` after save.

`config.toml.example` — `order` → `slot` in the panel blocks, with the 1–8 layout
in a comment above them.

---

## Change 7 — tests

`tests/test_system.py` (new):

- set/get round-trip; `get` on an absent key returns the default
- `snapshot()` returns a copy — mutating it does not affect subsequent reads
- concurrent writers from several threads leave the dict consistent

`tests/test_http_display.py`:

- `save_aircraft_array` publishes `tracked` matching the non-stale record count
- `tracked` reflects expiry — save, age the files past `STALE_SECONDS`, save again,
  assert the count dropped
- the SSE payload carries `system.tracked` at the top level
- `update()` stores the slot and the payload exposes it
- the panel dict carries `updated_epoch`

`tests/test_config.py`:

- missing `slot` fails
- `slot = 0` and `slot = 9` fail
- duplicate slots across two chains fail, and the message names both chains
- a panel block containing `order` fails with a message naming `slot`
- a valid eight-chain, eight-slot config loads

Client-side rendering is not under test and this brief does not add a JS test
harness for it. Verify the grid, the empty slots, the link dot and the header
count by running the wall and pulling the network cable.

---

## Explicitly out of scope

- **Panel layout renderers.** No `layout` key, no `list`/`count` renderers, no
  change to `renderCard`'s content beyond the age indicator and removing `⎈`.
  Next brief.
- **What each of the eight chains filters for.** Design decision, not code.
- **`render_aircraft_dict` serialising a list the card renders one of.** Intended:
  a chain may legitimately pass three aircraft to a display that shows the nearest.
  Filter output and display arity are independent by design.
- **Coalescing the SSE broadcast.** Every chain's `update()` currently rebuilds and
  sends the whole state. Wasteful at eight chains, not incorrect, and changing the
  broadcast model while also changing the payload shape is two risks at once.
- **Title-chip text wrapping on narrow panels.** Cosmetic, and belongs with the
  layout work that will change those chips anyway.
- **Any further `system` keys.** Ingestor poll times and adsbdb budget are the
  obvious next two. Neither has a consumer yet.
