# Brief: `band_closest` selector module

## Goal

Reduce the aircraft list to the nearest aircraft in each altitude band — one per
band, ordered highest band to lowest. This is what fills panel 1.

Depends on `location.altitude_band` being populated, which is the
`altitude_band` ingest enricher's job. This module contains no altitude logic
and no thresholds: it groups on the letter it is given.

---

## Module

New file `modules/band_closest.py`.

Behaviour:

1. Discard any aircraft whose `location.altitude_band` is `UNKNOWN`.
2. Discard any aircraft whose `location.distance_nm` is `UNKNOWN` — it cannot
   win a "nearest" comparison. Same rule as `closest_filter`.
3. Group the remainder by band letter.
4. From each group, take the aircraft with the lowest `distance_nm`.
5. Return them sorted by band letter **descending**, so `D, C, B, A` — the
   panel reads sky to ground.

Bands with no qualifying aircraft contribute nothing. The result is therefore
between zero and N entries, and the caller cannot tell which bands are missing
from position alone — it must read `location.altitude_band` on each returned
aircraft. That is what the tag is for, and the display brief will rely on it.

No config. `KEYS = {"type"}`. The block must still exist in `config.toml`
(`[modules.band_closest]`, empty) per the strict-validation rule, same as
`[modules.closest_filter]`.

---

## Placement in a chain

```toml
[processors.panel_one]
enabled               = true
poll_interval_seconds = 5
modules               = ["band_closest", "adsbdb"]
display               = "http"
```

`band_closest` runs before `adsbdb` deliberately: it cuts the list to at most
four aircraft, so the enricher performs at most four route lookups per cycle
instead of one per aircraft in range. This is the same "filter before enrich"
rule already documented for `adsbdb`, and here it is the difference between
roughly four lookups and roughly forty-five.

---

## Tests

New `tests/test_band_closest.py`:

1. One aircraft per band, all bands populated — returns four, in `D, C, B, A`
   order.
2. Several aircraft in one band — the lowest `distance_nm` wins.
3. A band with no aircraft is simply absent from the output; the remaining
   bands still return in descending order.
4. `altitude_band = UNKNOWN` excluded entirely, even with a valid distance.
5. `distance_nm = UNKNOWN` excluded, even with a valid band. A band containing
   only distance-less aircraft yields nothing for that band.
6. Empty input returns an empty list, no exception.
7. Every returned aircraft still carries its `altitude_band`, unmodified — the
   module selects, it does not enrich or strip.
8. Ties on `distance_nm` return exactly one aircraft, not two. Which one is
   unspecified, but the count is not.
9. Band letters beyond `D` sort correctly — build input with `A` through `F`
   and assert `F` leads. Guards against anything that assumes four bands.
10. Factory pooling — two references to `[modules.band_closest]` yield one
    instance.

---

## Docs

`docs/modules-reference.md` — new entry alongside `closest_filter`. State that
it is a selector rather than a filter, that it depends on the `altitude_band`
enricher running at ingest, that it holds no altitude thresholds of its own, and
that it should run before `adsbdb` for the reason above.

Note explicitly that an empty band produces no entry, and that consumers must
read `altitude_band` rather than infer position from list order.

---

## Next

The `list` renderer for the HTTP display: four fixed rows keyed by band letter,
each showing callsign, type and route, with an empty row where a band has no
aircraft. Row headings are panel config text, not derived from `edges`.
