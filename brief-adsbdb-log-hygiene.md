# Brief: dedup leak, non-ICAO addresses, and the unknown-type fallback

**Scope:** `modules/adsbdb.py`, `display/http/server.py`,
`docs/modules-reference.md`, `tests/test_module_adsbdb.py`,
`tests/test_http_display.py`.

**Do not** change `schemas/`, `ingestor/`, `storage/`, `processor/`, `config.py`,
or any filter module.

Three small independent fixes found by reading a live `unresolved.jsonl`. None
changes the pipeline's shape.

---

## Change 1 — the unresolved log deduplicates per process, not per memo window

**Observed.** In a 15-minute sample, five `(hex, callsign)` pairs each logged
twice, roughly four minutes apart: `SHT6C`, `SHT8K`, `AFR010`, `LHX1TW`, `FDX5034`.
All first appeared at 18:04 and again around 18:08.

The four-minute gap is the tell. `_MEMO_TTL_SECONDS` is 60, so the memo expires,
the route is re-attempted, it fails again, and a second line is written. The
deduplication set is being consulted somewhere inside the memo-guarded path rather
than at the point of writing, so it only suppresses repeats *within* a memo window.

The brief that introduced this asked for dedup "for the process lifetime". That is
the intended behaviour and it is what makes the log countable: 23 lines for 18
distinct callsigns means any budget estimate drawn from a line count is inflated by
about a quarter.

**Fix.** Check and update the set at the point the line is written, keyed on
`(hex, callsign)`, independent of the memo and its TTL. The set never expires; it is
bounded by distinct aircraft seen in one run, which is small.

Note this is not the same as caching. The not-found marker on disk correctly expires
so the route can be retried — a flight absent from adsbdb today may be there
tomorrow, and re-attempting is right. Only the *logging* is once-per-run. Keep those
two lifetimes separate and say so in a comment, because collapsing them is the
obvious wrong fix.

## Change 2 — don't look up non-ICAO addresses

readsb prefixes an address with `~` when it is not a real ICAO 24-bit address —
TIS-B relays, anonymised targets. There is no airframe behind it and never will be.
Two appeared in the sample (`~085ED5`, `~DC078E`), both with no registration.

Sending these to adsbdb is worse than wasteful. The call is a guaranteed miss, and a
404 writes a not-found marker keyed on a string that is not an aircraft identifier,
so the cache accumulates entries for things that cannot exist.

**Fix.** In `process()`, skip the aircraft entirely when `icao_hex` starts with `~` —
no aircraft call, no route call.

Do **not** write an unresolved-log line for these. The log records what adsbdb could
not resolve; a non-ICAO address was never a candidate. Logging it would be recording
a decision Squawk made, not a gap in adsbdb's data, and mixing those two makes the
log useless for the thing it exists to measure.

These aircraft still flow through the pipeline and still display — they simply carry
no enrichment. That is correct: they are real contacts, just unidentifiable ones.

## Change 3 — the unknown-type fallback is dead code

`render_aircraft_dict` substitutes an em-dash in Python:

```python
"aircraft_type": a.airframe.aircraft_type or "—",
```

and the page JS then tries to fall back:

```js
esc(a.aircraft_type || 'Unknown Airframe')
```

`"—"` is truthy, so the JS fallback never fires and the card shows a bare dash. Two
layers each think the other is handling it.

**Fix.** Send `null` from Python when the field is genuinely absent and let the
renderer own the display string. One fallback, in one place, reachable.

The airframe-fields brief has since changed these lines to `type_description` and
`type_code` — check whether the em-dash pattern survived the rewrite and apply the
same fix to whatever the current expression is. If the payload now emits `null`
correctly, this change is already done and the handback should say so rather than
inventing work.

Show **`Unknown type`** where a description and code are both missing. Not a dash,
not a blank: a wall panel showing a dash reads as a rendering fault, where explicit
text reads as an aircraft that did not identify itself.

**Route stays blank.** The card already omits the route box when there is no route,
and that is right — an empty space reads better than eight panels announcing
`UNKNOWN ROUTE`. Only the airframe line gets explicit text, because it is a labelled
field with a value slot that would otherwise sit visibly empty.

---

## Deliberately not changed

**`no_callsign` keeps being logged.** It is 52 of 75 lines in the sample and it is
noise — aircraft transmitting position before identity, each logged once on arrival
and resolving within a cycle or two. `4063DE` logged at 18:04 with no callsign and
enriched at 18:08 as `SHT19B`.

Suppressing it would mean tracking whether a hex later resolved, which is state and
logic in service of tidiness. Filtering when reading the log costs nothing:

```
grep unknown_callsign unresolved.jsonl
```

Worth a line in the docs saying `no_callsign` is expected and how to filter it out,
so the next reader is not misled by the volume.

---

## Change 4 — tests

- a repeated failing lookup of the same `(hex, callsign)` across a memo expiry logs
  exactly one line — advance the clock past `_MEMO_TTL_SECONDS` between attempts.
  This is the test that fails against current code.
- distinct callsigns for the same hex still log separately
- a `~`-prefixed hex triggers no HTTP call of either kind
- a `~`-prefixed hex writes no cache file and no log line
- a `~`-prefixed aircraft passes through `process()` unchanged rather than being
  dropped from the list
- the display payload carries `null`, not a placeholder, when type is unknown
- the renderer shows `Unknown type` for an aircraft with neither description nor code

---

## Explicitly out of scope

- **Filtering `~` addresses earlier**, at the converter or ingest. They are valid
  contacts and storage should keep them; only adsbdb has no use for them.
- **Publishing adsbdb's call rate to `system`.** Wanted, and a separate brief.
- **Any FlightAware or fallback-source work.**
- **The `no_callsign` volume**, as above.
