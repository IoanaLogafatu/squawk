# Brief: skip the SQLite lookup in `tar1090_db` when nothing's missing

## Goal

`tar1090_db.process()` guards its **writes** (`if a.airframe.registration
is None and reg:`) but not the **query** — `self._db.get(a.meta.icao_hex)`
runs for every aircraft with a hex, every cycle, regardless of whether
carry-forward already filled everything it would return. Once an
aircraft's `airframe` fields are populated, this is a wasted SQLite
`SELECT` on every cycle for as long as that hex stays in the pot — small
per call, but multiplied by full traffic volume every 5 seconds, it's
real sustained I/O for no reason, especially on Pi-class hardware. Same
fix shape `vrs_route` already has via `_needs_route()`; this brings
`tar1090_db` in line with it.

## Change

Add a pre-check, same convention as `_needs_route()`:

```python
def _needs_airframe(a: Aircraft) -> bool:
    """True if any field this module could fill is still UNKNOWN."""
    af = a.airframe
    return (
        af.registration     is None or
        af.type_code        is None or
        af.type_description is None or
        af.db_flags         is None
    )
```

In `process()`:

```python
for a in aircraft:
    if not a.meta.icao_hex:
        continue
    if not _needs_airframe(a):
        continue
    row = self._db.get(a.meta.icao_hex)
    ...
```

No change to the write guards below it — they stay exactly as they are,
as a correctness backstop for partial states (carry-forward filled
`registration` but not `db_flags`, say — an entirely plausible state if a
previous cycle's raw ADS-B supplied `registration` directly but
`tar1090_db` itself hadn't run yet). The new check only skips the *whole*
lookup when *every* field is already set; partial states still query and
still only write what's missing.

## Tests

New cases in `tests/test_tar1090_db.py`:

1. All four fields already populated — `self._db.get()` is **not**
   called. Assert call count, not just end state, same as the
   `vrs_route` carry-forward tests already do for `get_route()`.
2. Any single field still `None` — lookup still runs.
3. All fields `None` (first sighting) — lookup runs, behaves exactly as
   today.
4. A row found but only partially applied (some fields already filled
   from raw ADS-B or carry-forward) — only the still-`None` fields get
   written, matching existing guard behavior, now reached via the new
   gate rather than always.

## Docs

`docs/modules-reference.md`'s `tar1090_db` entry — one line noting the
pre-check, cross-referencing `vrs_route`'s identical pattern so the two
read as one deliberate convention rather than two independent fixes.
