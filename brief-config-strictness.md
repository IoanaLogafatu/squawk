# Brief: no silent config defaults

**Scope:** `config.py`, `config.toml`, `config.toml.example`, `modules/__init__.py`,
`modules/ground_distance_filter.py`, `modules/vertical_rate_filter.py`,
`processor/processor.py`, `README.md`, `docs/modules-guide.md`,
`docs/modules-reference.md`, `docs/display-guide.md`, `tests/test_config.py`.

**Do not** change any module's filtering logic, any display's rendering, `storage/`,
`ingestor/`, or `schemas/`.

> **Deploy note — this is a breaking config change.** Three deployments need their
> `config.toml` updated *before* the new code starts: the TV wall, the handheld e-paper
> Pi, and whichever host runs the pushover chain. Startup will fail with a clear error
> rather than misbehave, but it will fail. Update config first, then deploy.

> **Depends on `brief-module-factory-pooling.md`.** That brief keys the instance pool on
> `(module_type, cfg)`. With a config block now mandatory for every module reference,
> change it to `(name, cfg)` — the block name. One block, one object, stated plainly.
> Apply this brief after the pooling one, or apply the key change as part of this.

---

## Problem

Squawk fills in too much on the user's behalf. A config block that is missing, misnamed,
or misspelled does not fail — it falls through to a default and the system runs, wrongly
and silently.

The worst case is a filter that stops filtering:

```toml
modules = ["ground_distance_filter"]

[modules.ground_distance_filtr]     # typo
max_distance = 10
```

`config.modules.get("ground_distance_filter", {})` returns `{}`, the module imports
cleanly, `max_distance` and `min_distance` are both `None`, and every aircraft in the pot
passes. The panel fills with traffic 200nm away and nothing anywhere reports a problem.

The same shape recurs across the config: a chain with no `modules` runs and passes the
whole pot to its display; a chain with no `display` computes a result and discards it; an
http chain with no panel block gets `order = 999` and quietly stops honouring the grid
layout.

There is also a live inconsistency. `[processors.x]` defaults `enabled` to **true**;
`[ingestors.x]` defaults it to **false**. The same omission in two sections means
opposite things.

V1 is the moment to fix this, before anyone else's config exists.

---

## Principle

**Structure must be declared. Tuning may default.**

*Required* — anything whose absence changes what the system does or looks at:

- every block referenced by name (module, display, panel)
- `enabled`, `modules`, `display` on a processor chain
- `enabled` on an ingestor; `receivers` on `personal_adsb`
- `[squawk] data_dir`, `[storage] backend`, `[observer]`

*Still defaulted* — knobs with a sensible value that do not change the shape of the
pipeline: `poll_interval_seconds`, `timeout_seconds`, `port`, `title`, `order`,
`threshold`, `fallback`, `altitude_source`, `cooldown_seconds`, `full_refresh_every`,
`invert`.

The distinction matters: this brief must not end up demanding `fallback = true` on every
altitude filter. A block must exist; its optional keys stay optional.

---

## Change 1 — collect errors, report once

Before adding validation, add the mechanism. Config errors must be gathered and reported
together, not raised one at a time — otherwise fixing a config becomes a sequence of
restarts.

In `config.py`:

```python
class ConfigError(Exception):
    """Raised when config.toml is invalid. Message lists every problem found."""


def _fail(errors: list[str]) -> None:
    if errors:
        raise ConfigError(
            "config.toml has %d problem(s):\n\n  - %s"
            % (len(errors), "\n  - ".join(errors))
        )
```

`load_config()` accumulates into a single `errors` list across all the checks below and
calls `_fail(errors)` once, at the end, before constructing `SquawkConfig`.

Every message names the offending block and says what to do. Not `"missing key"` but
`"[processors.watchlist] has no 'display' key — set a display name, or set enabled = false"`.

---

## Change 2 — every referenced module needs a block

For each processor chain and each ingestor, every name in `modules` must have a
`[modules.<name>]` table. An empty table is valid and is the normal case for modules
that take no options:

```toml
[modules.closest_filter]
[modules.adsbdb]
[modules.tar1090_db]
```

Error text:

```
[processors.low_level] references module 'closest_filter' but there is no
[modules.closest_filter] block. Add one — it may be empty.
```

This is what catches the typo case in the Problem section: the block
`[modules.ground_distance_filtr]` is unreferenced and the reference
`ground_distance_filter` has no block, so both halves of the mistake are reported.

---

## Change 3 — every referenced display needs a block

Same rule for `display` on a processor chain. `display = "console"` requires
`[display.console]`, empty if the display takes no options.

```
[processors.pushover] uses display 'pushover' but there is no [display.pushover] block.
```

---

## Change 4 — every http chain needs a panel block

A chain whose `display = "http"` must have a `[display.http.panels.<chain_name>]` block.
`title` and `order` keep their defaults inside the block — the block itself is what is
required.

This closes the rename trap: renaming `[processors.watchlist]` currently orphans its
panel silently, which reverts it to a title-cased name at order 999 and reshuffles the
grid. Now it fails at startup.

```
[processors.watchlist] uses the http display but there is no
[display.http.panels.watchlist] block.
```

---

## Change 5 — required structural fields

**Processor chains.** `enabled`, `modules` and `display` all become required. Remove the
`p.get("enabled", True)`, `p.get("modules", [])` and `p.get("display")` defaults in
`_load_processors`. `poll_interval_seconds` keeps its default of 5.

A chain with an empty `modules = []` is legal — it passes the whole pot through, which is
a reasonable thing to want. It just has to be written down.

**Ingestors.** `enabled` becomes required, removing the true/false inconsistency between
the two sections. `personal_adsb` additionally requires `receivers`; an ingestor with no
receivers polls nothing forever and says nothing about it. `poll_interval_seconds` and
`timeout_seconds` keep their defaults.

Ingestor config stays a raw dict — do not build a typed `IngestorConfig`. Validate the
keys in the loader and leave the shape alone.

**Sections.** `[squawk]` with `data_dir`, `[storage]` with `backend`, and `[observer]`
with `latitude` and `longitude` all become required with named errors. `[observer]`
currently raises a bare `KeyError` from `raw["observer"]`, which is not a message anyone
should have to interpret.

---

## Change 6 — remove the legacy `[processor]` syntax

`_load_processors` supports a singular `[processor]` block alongside `[processors.<name>]`,
and `SquawkConfig` carries a `processor` property that returns "the first enabled one" as
a backwards-compatibility shim. Nine chains have been running for a while; nothing uses
either path except `processor.run()`'s `proc_cfg or config.processor` fallback, which
`main.py` never triggers because it always passes a chain.

This is the same class of thing as `SquawkEnvelope` — a second way to express something,
kept alive by tests, that a future reader might reasonably build on.

- Delete the legacy branch in `_load_processors`.
- Delete the `SquawkConfig.processor` property.
- In `processor/processor.py`, make `proc_cfg` a required argument and drop the
  `or config.processor` fallback and the `if cfg is None` guard.
- `README.md:96` documents the `[processor]` form — update it to `[processors.<name>]`.
- `tests/test_config.py` uses `config.processor` in four places and has a `[processor]`
  fixture at line 198. Retarget at a named chain.

---

## Change 7 — one spelling per config key

Two modules accept undocumented synonyms:

- `ground_distance_filter`: `max_distance` / `distance` / `within` / `below`, and
  `min_distance` / `above`, and `unit` / `units`
- `vertical_rate_filter`: `min_fpm` / `above`, `max_fpm` / `below`

Four spellings for one setting is four things to keep documented and consistent, and
`below` meaning "maximum distance" in one module and "maximum altitude" in another is
actively confusing. Keep one spelling each and delete the fallback chains:

- `ground_distance_filter`: `max_distance`, `min_distance`, `unit`
- `vertical_rate_filter`: `max_fpm`, `min_fpm`, `mode`, `threshold`

`docs/modules-reference.md` should document exactly these and no alternatives.

---

## Change 8 — warn on unreferenced blocks

The reverse check. A `[modules.x]` or `[display.x]` block that no chain or ingestor
references is dead config. Print a warning at startup rather than failing — an unused
block is untidy, not broken, and a user may be keeping one around while switching between
setups.

```
  config: [modules.ground_distance_filtr] is not referenced by any chain — ignored
```

Together with Change 2 this makes a misspelled block name produce two complementary
messages, one from each side.

---

## Change 9 — unknown keys inside a block

*This is the one change here that touches the module contract. It can be dropped without
affecting anything above.*

Changes 2–8 catch a wrong block *name*. They do not catch a wrong key *inside* a correct
block — `belwo = 5000` in `[modules.low_altitude]` still silently produces an unbounded
filter.

Cheapest fix that stays optional for module authors: a module may declare its keys at
module level, and `get_module()` warns about anything unrecognised.

```python
# modules/altitude_filter.py
KEYS = {"type", "above", "below", "altitude_source", "fallback"}
```

```python
# modules/__init__.py, in get_module, before construction
keys = getattr(module, "KEYS", None)
if keys is not None:
    unknown = set(cfg) - keys
    if unknown:
        print(f"  config: [modules.{name}] has unrecognised key(s): "
              f"{', '.join(sorted(unknown))}")
```

Warn, do not fail — a module that has not declared `KEYS` is unaffected, so this cannot
break a contributed module. Declare `KEYS` on all eight shipping modules and document it
in `docs/modules-guide.md` as recommended, not required.

---

## Change 10 — `config.toml.example` and docs

`config.toml.example` must satisfy every rule above, since it is what a new user copies.
That means adding the empty blocks (`[modules.closest_filter]`, `[modules.adsbdb]`,
`[modules.tar1090_db]`, `[display.console]` if referenced), an explicit `enabled` on every
ingestor and processor, and explicit `modules` and `display` on every chain.

It also currently contains `[modules.altitude_filter]` and `[modules.ground_distance_filter]`
blocks that exist only as documentation of the available options — under Change 8 those
would warn as unreferenced. Either reference them from a chain or move their contents into
comments in `docs/modules-reference.md` where option documentation belongs.

Add a short section to `docs/modules-guide.md` stating the rule: every module a chain
names needs a `[modules.<name>]` block, empty if it takes no options; one block means one
instance.

---

## Explicitly out of scope

- Type checking of config values (`port = "seven thousand"`). Structure only.
- Any change to what a module or display actually does.
- Removing or moving the concorde ingestor.
- The `from config import config` calls inside module `get()` functions.
- New modules, filters or layouts.

---

## Tests

`tests/test_config.py` grows a set of rejection tests. Each writes a minimal `config.toml`
to `tmp_path` and asserts `load_config()` raises `ConfigError` with the offending block
named in the message.

1. Chain references a module with no block → rejected, names the module.
2. Chain references a module whose block exists but is empty → **accepted**. This is the
   normal case and must not regress.
3. Chain references a display with no block → rejected.
4. Chain uses `display = "http"` with no matching panel block → rejected.
5. Chain missing `enabled` / `modules` / `display` → rejected, one test each.
6. Chain with `modules = []` and a valid display → **accepted**.
7. Ingestor missing `enabled` → rejected. `personal_adsb` missing `receivers` → rejected.
8. Missing `[squawk]`, `[storage]`, `[observer]` → rejected, one test each, each naming
   the section.
9. Multiple simultaneous problems → one `ConfigError` listing all of them, not the first.
10. Unreferenced `[modules.x]` block → loads successfully, warning printed (capsys).
11. Legacy `[processor]` block → no longer produces a chain.
12. `config.toml.example` itself loads without error. This is the test that stops the
    example drifting out of compliance.
13. Removed synonyms rejected or ignored per Change 7 — `[modules.x] within = 10` on a
    `ground_distance_filter` no longer sets a maximum.
14. Unknown key warning fires for a module declaring `KEYS`, and does not fire for one
    that does not.
