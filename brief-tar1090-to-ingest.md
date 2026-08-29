# Brief: move `tar1090_db` enrichment from the processor chains to the ingestors

**Scope:** `ingestor/__init__.py`, `ingestor/personal_adsb/ingestor.py`, `config.toml`,
`config.toml.example`, `docs/modules-reference.md`, `docs/modules-guide.md`, and `tests/`.

**Do not** touch `config.py`, `storage/`, `processor/`, `display/`, or
`modules/tar1090_db.py` itself.

> **Deploy note:** this brief changes both code and `config.toml`. They must ship
> together. Deploying the code without the config, or the reverse, leaves every panel
> unenriched with no error output — the same silent-failure mode as the earlier
> `registration_filter` change.

---

## Problem

`tar1090_db` is a hex → registration/type lookup against a local SQLite index. The
mapping is permanent, the lookup is free, and there is no rate budget.

It currently sits in all eight processor chains plus the pushover chain, so the same
aircraft is looked up once per chain per cycle. It also forces an ordering constraint on
any filter that reads registration or type: `registration_filter` only works because
`tar1090_db` runs ahead of it in the same chain.

Running it once at ingest fixes both. The general rule this establishes:

> **Static, free, local enrichment belongs at ingest. Rate-limited, ephemeral, external
> enrichment belongs in chains, after filtering.**

`adsbdb` stays in the chains — it is rate-limited and route data is per-flight with a
one-hour TTL. Enriching all ~200 aircraft in range instead of the handful that survive
filtering is exactly what its module docstring warns against.

---

## Change 1 — ingestor module helper

`config.ingestors` returns raw TOML dicts, so a `modules` key requires no `config.py`
change. Add a small helper to `ingestor/__init__.py`, which currently holds only a
docstring:

```python
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from modules import BaseModule


def get_ingest_modules(cfg: dict) -> list[BaseModule]:
    """Build the modules named in an ingestor's config block.

    These run against every aircraft before it enters storage, so they
    define what the whole installation can see — see docs/modules-guide.md.
    """
    from config import config
    from modules import get_module

    return [
        get_module(name, config.modules.get(name, {}))
        for name in cfg.get("modules", [])
    ]
```

This has two call sites today, so it is not speculative — but keep it to exactly this.
No new base class, no ingestor-specific module type, no registry.

---

## Change 2 — run the modules in `personal_adsb`

In `ingestor/personal_adsb/ingestor.py`, after the `get_storage(...)` line and before the
`while True:` loop:

```python
from ingestor import get_ingest_modules
ingest_modules = get_ingest_modules(cfg)
```

Then in the `if snapshots:` block, between building the envelope and saving:

```python
if snapshots:
    merged   = _merge_snapshots(snapshots)
    envelope = _build_envelope(merged, receiver_status)

    aircraft = envelope.aircraft
    for m in ingest_modules:
        aircraft = m.process(aircraft)

    storage.save_aircraft_array(aircraft)
```

Modules run **before** the save, so enrichment is baked into what is stored. This also
means `data/tracked_aircraft/*.json` now carries registration and type, which makes those
files far more useful when debugging by eye on the Pi.

**Do not modify `ingestor/concorde/ingestor.py`.** It sets `registration` and
`aircraft_type` from its own constants, so `tar1090_db` would have nothing to fill. Leave
its config block without a `modules` key.

---

## Change 3 — config

In both `config.toml` and `config.toml.example`:

Add to the `personal_adsb` ingestor block:

```toml
[ingestors.personal_adsb]
enabled = false
modules = ["tar1090_db"]      # enrichment applied to all aircraft entering storage
receivers = [ ... ]           # unchanged
```

Remove `"tar1090_db"` from the `modules` list of **all nine** processor chains. After the
change they read:

```toml
[processors.low_level]
modules = ["low_altitude", "closest_filter", "adsbdb"]
# ... and the same shape for mid_level, upper_level, high_cruise,
#     closest_overhead, watchlist, descending, climbing

[processors.pushover]
modules = ["ground_distance_filter", "registration_filter", "adsbdb"]
```

Note the pushover chain in particular: `registration_filter` now sits directly after the
distance filter with no enrichment stage ahead of it, because registration arrives from
ingest. That is the intended outcome.

---

## Change 4 — documentation

In `docs/modules-reference.md`:

- Remove the "requires upstream enrichment" note from `registration_filter`. It no longer
  applies — registration is present on every aircraft before any chain runs.
- Update the `tar1090_db` entry to say it is configured on the ingestor rather than in a
  processor chain, and note that its output is persisted to storage.

In `docs/modules-guide.md`, add a short subsection covering ingestor modules. Frame it
around **scope**, not around which module types are permitted:

> Modules listed on an ingestor define what enters storage. They run against every
> aircraft before it is saved, and every processor chain sees only what survives them —
> no chain can recover what was dropped.
>
> Enrichment here is cheap and applies once for the whole installation, which is why
> `tar1090_db` belongs on the ingestor rather than in each chain.
>
> A filter here is a deliberate choice to narrow the entire installation. A 20nm range
> filter is a reasonable configuration if you only care about aircraft visible from the
> window: storage stays small and every chain downstream is cheaper. Use it when that is
> what you mean. If you want a narrower view for a single panel, put the filter in that
> chain instead — a filter written on the ingestor will silently narrow all of them.

Do not add a type check or an allow-list. The pipeline is deliberately agnostic about
what a module does, and an ingest filter is a legitimate configuration rather than a
mistake to be guarded against.

---

## Explicitly out of scope

- Any change to `modules/tar1090_db.py`, including its singleton or its SQLite index.
- Moving `adsbdb` to ingest, or changing its position in any chain.
- Adding `modules` support to the concorde ingestor.
- Any new module type, base class, or ingestor/processor module distinction.

---

## Tests

1. **Helper builds modules from config** — `get_ingest_modules({"modules": ["pass_through"]})`
   returns one module; `get_ingest_modules({})` returns an empty list.

2. **Enrichment runs before save** — with a fake ingest module that sets a recognisable
   field, run one poll cycle against a `tmp_path` storage and assert the value is present
   in the JSON file on disk, not merely on the in-memory object.

3. **Empty modules list is a no-op** — one poll cycle with no `modules` key stores the
   same records as before the change.

4. **A filter at ingest narrows the pot** — with a filter module configured on the
   ingestor, run one poll cycle and assert only the surviving aircraft reach storage.
   This is asserting intended behaviour, not guarding against it.

5. **Chains no longer need `tar1090_db`** — update any existing test that builds a chain
   containing `tar1090_db` ahead of `registration_filter` so the filter receives
   pre-enriched aircraft instead. `tests/test_registration_filter.py` and
   `tests/test_integration_pipeline.py` are the likely candidates; check for others.

Existing `tests/test_tar1090_db.py` tests the module directly and should pass unchanged.

---

## Verification

- `./runtests.sh` passes.
- Deploy code and `config.toml` together, restart, and confirm all eight panels still
  show registration and aircraft type. Loss of type or registration across every panel
  means the ingestor `modules` key did not take effect.
- Confirm the pushover chain still matches watchlist registrations, since it now depends
  on ingest-time enrichment rather than in-chain enrichment.
- Open a file in `data/tracked_aircraft/` and confirm `registration` and `aircraft_type`
  are populated where the aircraft is in the tar1090 database.
- First start after deploy may pause while the SQLite index is built — this now happens
  in the ingestor thread rather than a processor thread. Expected, once.
