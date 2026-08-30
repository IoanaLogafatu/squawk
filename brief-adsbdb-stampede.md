# Brief: stop duplicate adsbdb lookups across chains

**Scope:** `modules/adsbdb.py`, `docs/modules-reference.md`,
`tests/test_module_adsbdb.py`.

**Do not** change the rate limits, the disk cache format or TTL, `_fetch`, `_apply`,
`_try_acquire`, or anything outside `modules/adsbdb.py`.

No config change. No new config keys.

---

## Problem

```
adsbdb: 200 for 40097D
adsbdb: 200 for 40097D
adsbdb: 200 for 40097D
```

That line only prints on an actual HTTP 200, so those are three live API calls for one
aircraft — three rate permits spent to fetch the same response.

`_get()` is not atomic. An aircraft that survives the filters in three chains is processed
by three threads. All three reach `_get()`, all three find no cache file (because none has
written one yet), all three call `_fetch()`, and all three then write the same file.

The instance pool made this visible but did not cause it. The disk cache has always been
shared, so the race existed with eight separate instances too — it was just harder to
attribute.

The disk cache prevents *repeat* calls across cycles. Nothing prevents *concurrent* calls
within one cycle, which is exactly when every chain wakes up.

---

## Design

Two additions to `AdsbdbEnricher`: an in-memory result cache, and a per-key lock so that
only the first thread to want a given aircraft does the work.

```python
_MEMO_TTL_SECONDS = 60
```

```python
def __init__(self, cache_dir: Path) -> None:
    self._cache_dir = cache_dir
    self._call_times: deque[float] = deque()
    self._rate_lock = threading.Lock()

    self._memo: dict[tuple[str, str | None], tuple[float, dict | None]] = {}
    self._key_locks: dict[tuple[str, str | None], threading.Lock] = {}
    self._memo_lock = threading.Lock()
```

`_get()` keeps its entire body, renamed to `_get_uncached()`. The new `_get()` wraps it:

```python
def _get(self, hex_id: str, callsign: str | None) -> Optional[dict]:
    key = (hex_id, callsign)
    now = time.monotonic()

    with self._memo_lock:
        entry = self._memo.get(key)
        if entry is not None and now - entry[0] <= _MEMO_TTL_SECONDS:
            return entry[1]
        key_lock = self._key_locks.setdefault(key, threading.Lock())

    with key_lock:
        # Re-check: another thread may have filled the memo while we waited
        # on the lock, which is the whole point of this method.
        with self._memo_lock:
            entry = self._memo.get(key)
            if entry is not None and time.monotonic() - entry[0] <= _MEMO_TTL_SECONDS:
                return entry[1]

        result = self._get_uncached(hex_id, callsign)

        with self._memo_lock:
            self._memo[key] = (time.monotonic(), result)
        return result
```

Three points on this.

**The re-check inside the key lock is load-bearing.** Without it the second thread waits
for the first, then does the fetch anyway — the exact bug, just slower.

**Key on `(hex_id, callsign)`, not hex alone.** `_get_uncached()` behaves differently with
and without a callsign: a cached entry lacking a `flightroute` block triggers a re-fetch
when a callsign is available. Keying on hex alone would let a callsign-less lookup poison
the result for a callsign-bearing one. All chains see the same `Aircraft` object from the
shared snapshot, so the callsign is consistent across them for any given cycle.

**Failures are memoised too.** `result` may be `None` — rate limited, network error,
malformed response. Caching that for 60 seconds is deliberate: without it, a rate-limited
aircraft would be retried by all nine chains every cycle, which is the stampede again in
its worst form. The disk cache already handles 404s separately over the full hour.

**Why 60 seconds rather than the disk cache's hour.** It only has to span one poll cycle
plus jitter; the disk cache still does the long-term job. A short TTL keeps memory bounded,
lets a manually deleted cache file take effect within a minute, and roughly matches
storage's own 60-second aircraft expiry, so an aircraft leaving the pot has its entry
expire at about the same time.

---

## Change 2 — bound the dictionaries

Both dicts grow with every distinct aircraft seen. Sweep expired entries at the top of
`process()`, which runs once per chain per cycle:

```python
def _sweep(self) -> None:
    now = time.monotonic()
    with self._memo_lock:
        dead = [k for k, (t, _) in self._memo.items()
                if now - t > _MEMO_TTL_SECONDS]
        for k in dead:
            del self._memo[k]
            self._key_locks.pop(k, None)
```

Called as the first line of `process()`. The memo holds at most one minute of distinct
aircraft, so the sweep is over a few hundred entries at worst.

Dropping a key lock that a thread is currently waiting on is safe — the waiter holds its
own reference and will still acquire it. The only consequence is that a thread arriving
immediately afterwards creates a fresh lock for the same key, so in a narrow window two
threads could both fetch. That is the pre-existing behaviour, occurring far less often, and
is not worth further machinery.

---

## Change 3 — documentation

`docs/modules-reference.md`, in the `adsbdb` entry, and the module docstring's Cache
section. Both currently describe only the disk cache. Add:

> **In-memory cache (60 seconds).** Sits in front of the disk cache. When several chains
> process the same aircraft in the same cycle, one performs the lookup and the rest reuse
> its result. Failed and rate-limited lookups are cached for the same window so they are
> not retried by every chain.

---

## Explicitly out of scope

- Changing `_CACHE_TTL_SECONDS`, the rate limits, or the disk cache layout.
- Persisting adsbdb enrichment to storage. Enrichment currently happens per chain and is
  discarded when the snapshot is re-read. Whether it should move to ingest, as
  `tar1090_db` did, is a separate question — and the answer is not obviously yes, since
  adsbdb is rate-limited and benefits from running after filters.
- Applying the same treatment to `tar1090_db`. Its lookups are local SQLite point queries
  with thread-local connections; there is no stampede to prevent.
- Any change to `_apply`, or to which fields are populated.

---

## Tests

`tests/test_module_adsbdb.py`:

1. **Concurrent lookups produce one fetch** — patch `_fetch` with a counting stub that
   sleeps briefly, run three threads calling `_get("40097D", "EZY123")` simultaneously,
   assert the counter is 1 and all three receive the same result. This is the bug; it must
   fail against the current code.
2. **Memo hit avoids disk** — after one `_get()`, delete the cache file, call `_get()`
   again, and assert the result still arrives and `_fetch` was not called again.
3. **Memo expiry** — monkeypatch `time.monotonic` to jump past `_MEMO_TTL_SECONDS`, assert
   the next `_get()` re-enters `_get_uncached()`.
4. **Failures are memoised** — `_fetch` returning `None` results in one call across three
   concurrent lookups, not three.
5. **Different callsigns are distinct keys** — `_get("40097D", "EZY123")` and
   `_get("40097D", None)` each perform their own lookup.
6. **Sweep bounds the dicts** — after entries expire and `process()` runs, `_memo` and
   `_key_locks` are empty.
7. **Existing disk-cache and rate-limit tests still pass unchanged.** The memo sits in
   front of that logic; none of it moves.
