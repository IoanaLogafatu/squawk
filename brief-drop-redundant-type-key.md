# Brief: drop redundant `type` from each module's own `KEYS`

## Context

The `data_sources` brief added `_COMMON_KEYS = {"type", "source"}` at the
factory level in `modules/__init__.py`, so neither key needs to appear in any
individual module's `KEYS` for the unknown-key check to pass. `source` never
needed adding to any module — it just worked. `type` still appears in all nine
modules' `KEYS`, left alone deliberately at the time to avoid churning nine
files inside a brief that didn't ask for it.

That's this brief.

## Change

1. Confirm `get_module`'s unknown-key check subtracts `_COMMON_KEYS` as well
   as the module's own `KEYS` — i.e. `unknown = set(cfg) - keys - _COMMON_KEYS`
   (or equivalent). This should already be true, since `source` currently
   works without any module declaring it. If it isn't quite that shape, fix
   the check first — everything below depends on it.

2. Remove `"type"` from the `KEYS` set in all nine modules:
   `altitude_band`, `altitude_filter`, `band_closest`, `closest_filter`,
   `adsbdb`, `tar1090_db`, `ground_distance_filter`, `registration_filter`,
   `vertical_rate_filter` — check the actual module list for any missed, this
   is from memory of tonight's session, not a fresh listing.

3. Update `tests/test_modules.py::test_every_module_declares_keys` (or
   whatever it's actually called) so it no longer requires `"type"` in each
   module's own `KEYS`. Decide what it should assert instead — options:
   - Drop the `type` check entirely, since it's now a factory-level guarantee
     rather than a per-module one.
   - Keep a single assertion that `_COMMON_KEYS` contains `type` and `source`,
     proving the guarantee exists at the one place it now lives, rather than
     asserting it nine times.

   The second is probably better: it preserves the *intent* of the original
   test (nothing silently accepts an unrecognised `type` key) while pointing
   at the actual mechanism instead of a convention nine files have to remember.

## Non-goals

No change to `_COMMON_KEYS` itself, no change to what config any module
accepts — this only removes a now-meaningless duplicate declaration. If a
tenth module is added later, it should not need to list `type` in its own
`KEYS` either; that's the whole point.

## Tests

- `test_every_module_declares_keys` (or its replacement) passes with `type`
  removed from all nine `KEYS` sets.
- Existing tests asserting an unknown key is rejected (e.g. a module given
  `foo = "bar"` in its config block) still pass — proves the factory-level
  subtraction, not per-module declaration, is what's actually doing the work.
- A quick sanity check: a module's own config block genuinely lacking `type`
  in `KEYS` still resolves correctly via `cfg.get("type", cfg.get("module", name))`
  in `get_module` — unaffected by this change, but worth a one-line assertion
  that resolution still works end to end for at least one module, since this
  is the path that would break silently if the two were ever coupled.

## Docs

`docs/modules-guide.md` — if it documents `KEYS` and tells module authors to
include `type`, correct that instruction: `type` and `source` are handled by
the factory now, a module's `KEYS` should list only its own config keys.
