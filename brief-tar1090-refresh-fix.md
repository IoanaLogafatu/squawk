# Brief: `tar1090_db` periodic refresh check

## Bug

`Tar1090DbEnricher` checks the CSV's age only when the module is constructed,
which happens once per process start (module instances are pooled per
`(name, cfg)` key and live for the life of the process). A long-lived Squawk
process — weeks or months without a restart — never re-checks, and so never
re-downloads, however old the cached CSV gets. `refresh_days` currently caps
staleness in theory only; in practice staleness is capped by how often the
service happens to restart.

## Fix

Move the age check out of construction and into `process()`, gated so it only
actually runs once an hour rather than on every poll cycle.

Rationale for the hour, not the poll interval: at a 5-second poll this would
otherwise run roughly 17,000 times a day to answer a question that changes
once a month — cheap per call, wasteful in aggregate, and needless disk I/O on
SD-card hardware. An hour keeps the 30-day threshold effectively exact (an
hour of slack on 30 days is nothing) while cutting the check rate by roughly
700×. It also means a receiver that's powered off overnight picks up a
pending refresh within the hour of coming back, rather than waiting for the
next full day.

```python
_CHECK_INTERVAL_SECONDS = 3600

class Tar1090DbEnricher(BaseModule):
    def __init__(self, ...):
        ...
        self._last_check = 0.0   # force a check on first process() call

    def process(self, aircraft: list[Aircraft]) -> list[Aircraft]:
        now = time.time()
        if now - self._last_check >= _CHECK_INTERVAL_SECONDS:
            self._maybe_refresh()
            self._last_check = now
        ...
```

`_maybe_refresh()` is the existing download-if-stale logic already in the
module (age vs. `refresh_days`, re-download, rebuild the SQLite index) —
extract it into its own method if it currently lives inline in `__init__` or
`get()`, so `process()` can call the same logic construction used to.

`refresh_days` itself is unchanged — still the maximum age before a download
happens, default 30, configurable exactly as today. Nothing about the
threshold changes; only how often it's checked.

## Config

No new config. `refresh_days` already exists; confirm it stays configurable:

```toml
[modules.tar1090_db]
refresh_days = 30    # unchanged default
```

## Concurrency note

If `tar1090_db` is referenced by more than one chain, the factory pools it to
one instance, so only one `process()` call stream will ever trigger the hourly
check — no risk of two chains racing to redownload the same file
simultaneously. Worth a one-line assertion or comment confirming this rather
than assuming it, since a future refactor to per-chain instances would
reintroduce the race.

## Tests

Extend `tests/test_tar1090_db.py`:

1. First call to `process()` triggers a refresh check regardless of any
   `_last_check` state (construction no longer performs the check itself).
2. A second `process()` call within the hour does not re-check — mock the
   refresh method and assert it's called once across several rapid
   `process()` calls.
3. A `process()` call after the interval has elapsed re-checks.
4. The refresh-if-stale decision itself (age vs. `refresh_days`) is unchanged
   in behaviour — existing tests for that logic should keep passing unmodified
   once the logic is extracted into its own method.
5. Simulate a long-lived instance: several `process()` calls spread across a
   simulated multi-day gap (patch `time.time`) correctly triggers a download
   once the file exceeds `refresh_days`, proving the original bug is fixed.

## Docs

`docs/modules-reference.md` — update the `tar1090_db` entry to say the refresh
check runs continuously (hourly) rather than only at startup, so a long-running
process still picks up updates without needing a restart.
