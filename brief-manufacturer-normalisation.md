# Brief: manufacturer normalisation

## Goal

Show the brand name a viewer recognises rather than the registered legal entity
adsbdb returns. `Boeing Company 737-800` becomes `Boeing 737-800`;
`Airbus Sas A321-251NX` becomes `Airbus A321-251NX`.

This is a display-layer transform. `airframe.manufacturer` keeps whatever the
source gave it — the renderer decides what to draw.

---

## Why

The four-branch manufacturer rule in `_type_label` is working as specified. The
defect is upstream of it: adsbdb's `manufacturer` field holds the corporate
entity, so rule 4 faithfully concatenates `Boeing Company` or `Airbus Sas` onto
the type. The result is both wrong to read and long enough to truncate the
variant code that would have been the interesting part of the line.

---

## Change

In `display/http/server.py`, beside `_short_country`, add `_short_manufacturer`
and apply it to the manufacturer **before** `_type_label` runs. The four-branch
rule is unchanged.

Order matters: normalising first means rule 3's case-insensitive prefix check
compares `Boeing` against `BOEING 737-800` and strips correctly. Normalising
after would leave rule 3 comparing `Boeing Company` against `BOEING 737-800`,
missing, and falling through to rule 4.

An explicit map, same shape as `_short_country` — not a general suffix-stripping
scheme. A rule that removes trailing corporate words would mangle manufacturers
whose brand legitimately contains one.

Seed it from what is actually in the cache rather than from guesswork:

```
jq -r '.aircraft.manufacturer' data/modules/adsbdb/aircraft/*.json | sort | uniq -c | sort -rn
```

Include at minimum the forms seen on the wall so far — `Boeing Company` and
`Airbus Sas` — plus anything else the count turns up with a corporate suffix.
Match case-insensitively; store the brand in its display casing. Unmapped values
pass through untouched.

---

## Also: compound municipalities

adsbdb occasionally returns a municipality like `Cincinnati / Covington`, which
truncates on the wall. Split on `/` and take the first part, stripped. Applies
to both origin and destination.

Same file, applied where the municipalities enter the payload.

---

## Tests

Extend `tests/test_http_display.py`:

1. `Boeing Company` → `Boeing`; `Airbus Sas` → `Airbus`.
2. Case-insensitive match — `BOEING COMPANY` maps too.
3. An unmapped manufacturer passes through unchanged.
4. `None` passes through, no exception.
5. Normalisation runs before `_type_label`: `Boeing Company` +
   `BOEING 737-800` yields `Boeing 737-800`, not `Boeing Company BOEING 737-800`
   and not `Boeing BOEING 737-800`. This is the ordering guard and the reason
   the brief exists.
6. `Cincinnati / Covington` → `Cincinnati`, both ends.
7. A municipality with no slash is unchanged, including one with spaces.

The existing `De Havilland Canada` doubling test must still pass — normalisation
does not fix it and is not meant to.

---

## Docs

`docs/display-guide.md` — in the manufacturer rule section, note that the
manufacturer is normalised to its brand name before the rule runs, why adsbdb's
value needs it, and that the map is explicit rather than a suffix-stripping
heuristic.
