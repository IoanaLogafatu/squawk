# Display Modules — Developer Guide

A **display** is a module that consumes the aircraft list and writes it somewhere a human can see — a screen, a web page, a terminal, an LED strip. Mechanically, displays are not a separate type: they implement the same `BaseModule` interface as filters and enrichments and occupy the same kind of slot in the chain. They are documented separately because they are the most common thing a Squawk user will want to build for themselves.

This guide covers the conventions, three worked examples (`console`, `http`, `epaper`), and a checklist for writing your own.

## What makes a module a "display"

By convention, a display:

- Reads from the aircraft list and produces side effects — writes to a screen, sends to clients, draws to hardware.
- Returns its input list **unchanged**, so subsequent modules still see the same data.
- Lives near the end of the chain, after filters have reduced the list to what should actually be shown.
- Handles the empty list gracefully — "no aircraft" is a normal state, not an error.
- Handles `UNKNOWN` (`None`) fields gracefully — registration, type, distance and the rest are all individually optional.

None of this is enforced. A display that mutates the list or sits mid-chain will run; it just won't behave like the rest.

## Position in the chain

Displays almost always come last. The usual pattern:

```toml
[[processor.chain]]
name = "route_enrichment"

[[processor.chain]]
name = "closest_filter"      # reduce to one aircraft

[[processor.chain]]
name = "epaper_display"      # display whatever the filter left
```

Stacking multiple displays in one chain works as expected — e-paper and HTTP simultaneously, both seeing the same one aircraft.

## Worked examples

### console — the minimal case

```python
class ConsoleDisplay(BaseModule):
    def process(self, aircraft: list[Aircraft]) -> list[Aircraft]:
        if not aircraft:
            print("  ○ no aircraft")
        else:
            a   = aircraft[0]
            reg = a.static.registration  or "???"
            typ = a.static.aircraft_type or "???"
            print(f"{reg}  {typ}")
        return aircraft
```

The full shape of a display in eight lines. Things to notice:

- Empty-list branch handled explicitly.
- `aircraft[0]` is the convention: assume the chain has already filtered to the target. If it hasn't, you'll show whichever aircraft happens to be first.
- `UNKNOWN` fields guarded with `or "???"`.
- Input list returned unchanged.

If you've never written a module before, start by copying this and swapping `print()` for your sink of choice.

### http — background server with shared state

The HTTP display serves a live web page that auto-updates as new aircraft arrive. `process()` is still trivial; the real work happens in the constructor.

```python
class HttpDisplay(BaseModule):
    def __init__(self, cfg: dict) -> None:
        port        = int(cfg.get("port", 7700))
        self._state = SharedState()
        server      = ThreadingHTTPServer(("", port), make_handler(self._state))
        threading.Thread(target=server.serve_forever, daemon=True).start()

    def process(self, aircraft: list[Aircraft]) -> list[Aircraft]:
        self._state.update(aircraft[0] if aircraft else None)
        return aircraft
```

The pattern: anything that needs to live outside the `process()` call — a server, a worker thread, a hardware handle — is built in `__init__` and stashed on `self`. `process()` pokes the shared state and returns immediately. The pipeline never blocks on an HTTP client.

Notable details:

- `daemon=True` so the server thread dies when the pipeline shuts down.
- `SharedState` is a small thread-safe pub/sub between the module thread (writer) and request handler threads (readers). HTTP clients are not the module's problem; they read from the shared state independently.
- Passing `None` when the list is empty is part of the contract — the renderer downstream knows what to do with it.

### epaper — hardware, with throttling

E-paper screens have constraints the other examples don't: a full refresh takes ~2 seconds, panels wear with repeated writes, and the SPI bus doesn't tolerate concurrent access. The module interface is unchanged, but the implementation has to respect the hardware.

The pattern for any slow or wear-sensitive sink:

- **De-duplicate.** Keep `self._last_rendered` and compare. If the new value matches the last, return without touching the screen.
- **Throttle.** Track `self._last_draw_time`. Enforce a minimum interval between writes regardless of how often `process()` is called.
- **Long-lived handle.** Initialise the e-paper driver in `__init__` and reuse it. Opening the SPI bus per cycle is both slow and a good way to deadlock against your own previous call.

From outside, the module still looks like `process(aircraft) -> aircraft`. The complexity is internal — exactly as it should be.

Hardware caveat for systemd on the Pi: the service user must be in the `spi`, `gpio`, and `i2c` groups, or the module will work interactively but fail when run as a service.

## Writing your own — checklist

1. **Subclass `BaseModule`** and implement `process(aircraft) -> aircraft`.
2. **Add the factory:** `def get(cfg: dict) -> YourDisplay`.
3. **Always return the list.** Even when you've done nothing else. Returning `None` breaks every module after you.
4. **Handle the empty list.** It will happen, often. Decide what your display shows when there's nothing overhead.
5. **Guard `UNKNOWN` fields.** Every field on every aircraft can be `None`. Decide your fallback (`"—"`, `"???"`, omitting the row).
6. **Long-lived resources in `__init__`.** Servers, hardware handles, open files, threads — build once, reuse.
7. **Slow output stays out of `process()`.** Anything that waits on a client, screen, or network call should run on a background thread reading from shared state. The processor doesn't know your output is expensive.
8. **De-duplicate and throttle if writes are costly.** Hardware especially.
9. **Don't raise.** A display crash should not take the pipeline down. Wrap risky I/O in `try/except` and log.

## Testing

Display modules are easy to test because `process()` is a pure function with side effects:

- Construct the module with a config dict.
- Hand it a list of `Aircraft` objects (real or built in-test).
- Assert on the side effect (captured stdout, mock screen calls, HTTP fetch against the bound port).
- Hand it `[]` and assert the empty branch behaves.
- Hand it aircraft with various `UNKNOWN` fields and assert nothing crashes.

For hardware-backed displays, mock the driver at construction time — the module should accept a driver object via cfg (or via dependency injection in tests) rather than importing it at module level.

## Common pitfalls

- **Reading more than `aircraft[0]`** without making your assumption explicit. If you want to render the whole list, fine — but document it, because the next person to add a filter upstream will be surprised when their "single closest" result gets replaced by a scrolling list.
- **Forgetting to return.** The single most common bug. Always `return aircraft` at the bottom of `process()`.
- **Blocking in `process()`.** Anything that waits — a network client, a slow disk, a screen refresh — holds up the next ingestor cycle. Push it to a background thread.
- **Treating `None` as an error.** `UNKNOWN` is normal. Plan for it in every field you render.
- **Re-opening hardware per cycle.** Always a bug. Build the handle once, keep it on `self`.
