# Brief: share one `adsbdb` instance and its rate limiter across all processor chains

**Scope:** `modules/adsbdb.py` and `tests/test_module_adsbdb.py` only. Do not touch
`processor/`, `config.py`, `config.toml`, or any other module.

---

## Problem

`modules/adsbdb.py:get()` returns a fresh `AdsbdbEnricher` on every call. The rolling
rate-limit deque `_call_times` is an instance attribute, so every processor chain that
names `adsbdb` gets its own independent limiter.

The live `config.toml` has eight enabled chains all ending in `adsbdb`. The documented
limits (512 calls / 60s, 1024 calls / 300s) are therefore enforced eight times
independently — a worst case of roughly 4096 calls/60s from a single home IP against
a free, no-auth public API.

There is a second, separate bug in the same area. `_get()` checks `_under_rate_limit()`
once and can then issue **two** fetches: first with the callsign, then the hex-only
fallback. Each calls `_record_call()`. One permit, two API calls. This exists today even
single-threaded.

---

## Change 1 — module-level shared instance

Add a module-level singleton guarded by a lock, mirroring the pattern already used in
`modules/tar1090_db.py`:

```python
_INSTANCE: AdsbdbEnricher | None = None
_INSTANCE_LOCK = threading.Lock()


def get(cfg: dict) -> AdsbdbEnricher:
    global _INSTANCE
    with _INSTANCE_LOCK:
        if _INSTANCE is None:
            from config import config as squawk_config
            data_dir  = Path(squawk_config.squawk.data_dir)
            cache_dir = data_dir / "modules" / "adsbdb"
            cache_dir.mkdir(parents=True, exist_ok=True)
            _INSTANCE = AdsbdbEnricher(cache_dir=cache_dir)
    return _INSTANCE
```

**Do not** build a config-keyed instance pool. `get()` currently ignores `cfg` entirely —
there is no per-chain configuration to key on, and a pool would be unused abstraction.

**Do not** change the `AdsbdbEnricher.__init__` signature. It must remain directly
constructible as `AdsbdbEnricher(cache_dir=...)`, because the existing tests build it
that way and inspect `_call_times` directly. The sharing lives in `get()`, not in the
class.

---

## Change 2 — replace the two rate-limit methods with one atomic operation

Delete `_under_rate_limit()` and `_record_call()`. Replace them with a single
`_try_acquire()` that prunes, checks both windows, and records the call as one
lock-protected operation:

```python
def _try_acquire(self) -> bool:
    """Reserve one API call slot. Returns False if either rate limit is reached."""
    with self._rate_lock:
        now = time.monotonic()
        while self._call_times and now - self._call_times[0] > 300:
            self._call_times.popleft()
        if len(self._call_times) >= _RATE_300S:
            return False
        if sum(1 for t in self._call_times if now - t <= 60) >= _RATE_60S:
            return False
        self._call_times.append(now)
        return True
```

Add `self._rate_lock = threading.Lock()` in `__init__`. Keep `_call_times` as a
`deque` instance attribute under its current name.

Note this records the call *before* the request is issued, which is the correct order —
a call that is attempted counts against the limit whether or not it succeeds. The current
code already records on both the success and exception paths of `_fetch`, so this is not
a behaviour change.

---

## Change 3 — move the guard into `_fetch`

`_fetch()` is the only place an HTTP request is made, so the guard belongs there. This
makes it structurally impossible for a caller to bypass the limiter, and fixes the
one-permit-two-calls bug without any caller needing to know about it.

At the top of `_fetch()`, before building the request:

```python
if not self._try_acquire():
    print(f"  adsbdb: rate limit reached — skipping {lbl}")
    return None
```

Then remove the existing `self._record_call()` calls from both the exception path and
the success path of `_fetch()`.

In `_get()`, remove both caller-side rate-limit checks:

- The `and self._under_rate_limit()` clause in the stale-flightroute refresh condition
  (the `if callsign and "flightroute" not in cached_data ...` branch) — just drop that
  clause, leaving the callsign and flightroute conditions.
- The `if not self._under_rate_limit(): ... return cached_data ...` early return before
  the fetch attempts — delete the whole block.

Behaviour is preserved in both cases: a denied fetch now returns `None`, and `_get()`
already falls through to returning stale cache or `None` when a fetch returns `None`.
The only difference is which log line prints.

---

## Explicitly out of scope

Do not add any of the following as part of this change:

- An in-memory result cache in front of the on-disk JSON cache.
- Any change to the cache file format, TTL, or directory layout.
- Any change to `_apply()` or the schema fields it populates.
- Any change to `modules/tar1090_db.py`, even though it has a related pattern.

---

## Tests

Add to `tests/test_module_adsbdb.py`:

1. **Shared instance** — two calls to `modules.adsbdb.get({})` return the same object
   (`is` identity). Reset the module-level `_INSTANCE` to `None` in a fixture so the test
   is order-independent, and restore it afterwards.

2. **Limiter is shared** — obtain the enricher twice via `get()`, fill `_call_times` to
   `_RATE_60S` through one reference, then assert `_try_acquire()` returns `False` through
   the other.

3. **One permit per fetch** — with `requests.get` mocked to raise, call `_fetch` twice on
   a fresh enricher and assert `len(_call_times) == 2`. Then fill the deque to the limit
   and assert a further `_fetch` returns `None` and does not call `requests.get` at all.

Existing tests that construct `AdsbdbEnricher(cache_dir=tmp_path)` directly and append to
`_call_times` must continue to pass unchanged. Any existing test that calls
`_under_rate_limit()` or `_record_call()` by name should be updated to `_try_acquire()`,
keeping the same assertion intent.

---

## Verification

- `./runtests.sh` passes.
- Start Squawk with the live eight-chain `config.toml` and confirm the startup output
  still shows all chains starting, with no duplicated adsbdb initialisation.
- Confirm `data/modules/adsbdb/` is still being written to and read from as before.
