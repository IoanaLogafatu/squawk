# Squawk

**A modular aircraft tracking pipeline.**

Squawk turns a personal ADS-B receiver into a live data stream of the aircraft *you* care about. Real-time surveillance data is ingested from your own receivers, passed through a configurable chain of filter and enrichment plugins, and handed off to the display of your choice — a web page, an e-paper screen, or whatever you build next. Ingestors can query external APIs, such as FlightAware tracking (for a mobile experience) or other data sources, such as weather update, even a local flying club timetable.

The whole system is built around plugins. Want to track only aircraft within 20 nm? Drop in a filter. Want to look up routes from an external API? Drop in an enricher. Want to send the result to a Slack channel or a handheld display? Write a display plugin. The processor doesn't care what each step does, only that it accepts `list[Aircraft]` and returns `list[Aircraft]`.

## Pipeline

```
[ Ingestors ] → [ Storage ] → [ Processor ]→ [ Plugin Chain ] → [ Display ]
```

- **Ingestors** poll external sources (your tar1090 receiver, the bundled Concorde simulator, anything you write) and emit a `SquawkEnvelope` per cycle.
- **Storage** persist envelopes through a pluggable storage backend.
- **The processor** reads the current snapshot on its own schedule, runs the configured plugin chain, then hands the result to a display.
- **Plugins** filter or enrich. They all share one interface: `list[Aircraft] → list[Aircraft]`.

Each ingestor runs on its own thread, so sources with different poll intervals don't block each other.

## Quick start

```bash
git clone https://github.com/IoanaLogafatu/squawk.git
cd squawk
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.toml.example config.toml
# edit config.toml — at minimum, set [observer] coordinates
python main.py
```

With the default config, the Concorde simulator flies G-BOAC overhead on a random cardinal bearing and the HTTP display is served at <http://localhost:7700>.

> Full installation instructions, plus a walkthrough for setting Squawk up on a handheld Raspberry Pi Zero 2 with a Waveshare e-paper display, are on the [wiki](https://github.com/IoanaLogafatu/squawk/wiki).

## Configuration

Everything lives in `config.toml`. Each section enables or disables one component:

```toml
[observer]
latitude  = 53.7778
longitude = -1.5721

[ingestors.personal_adsb]
enabled   = true
receivers = [
    { name = "receiver-one", url = "http://receiver-one.local/tar1090/data/aircraft.json" },
]
poll_interval_seconds = 5

[processor]
poll_interval_seconds = 5
plugins               = ["tar1090_db", "closest_filter"]
display               = "http"

[display.http]
port = 7700
```

`config.toml` itself is gitignored. `config.toml.example` is the reference kept in version control — keep it in sync when adding new keys.


## Writing your own plugin

```python
# plugins/altitude_floor.py
from plugins import BasePlugin
from schemas.aircraft import Aircraft

class AltitudeFloor(BasePlugin):
    def __init__(self, min_feet: int) -> None:
        self._min_feet = min_feet

    def process(self, aircraft: list[Aircraft]) -> list[Aircraft]:
        return [a for a in aircraft if (a.dynamic.altitude_feet or 0) >= self._min_feet]

def get(cfg: dict) -> AltitudeFloor:
    return AltitudeFloor(min_feet=cfg.get("min_feet", 5000))
```

Wire it into the chain:

```toml
[processor]
plugins = ["altitude_floor", "closest_filter"]

[plugins.altitude_floor]
min_feet = 10000
```

The plugin is discovered by name — no registration step. See [`docs/plugins-guide.md`](docs/plugins-guide.md) for the full contract.

## Tests

```bash
./runtests.sh
```

## Documentation

- [`docs/plugins-guide.md`](docs/plugins-guide.md) — writing filter and enrichment plugins
- [`docs/display-guide.md`](docs/display-guide.md) — writing display plugins
- [`docs/storage-guide.md`](docs/storage-guide.md) — writing a new storage backend
- [`docs/primary_ingestor.md`](docs/primary_ingestor.md) — design notes on `personal_adsb`

## Acknowledgements

Squawk is a hobby project that leans heavily on free public data sources.
Please honour their terms.

| Source                               | Used by                  | Notes                                                                                                                       |
| ------------------------------------ | ------------------------ | --------------------------------------------------------------------------------------------------------------------------- |
| [wiedehopf/tar1090-db][tar1090-db]   | `tar1090_db` plugin      | Aircraft registration and type database.                                                                                    |
| [adsbdb.com][adsbdb]                 | `adsbdb` plugin          | API that aggregates the three sources below.                                                                                |
| &nbsp;&nbsp;↳ [Planebase][planebase] | (via adsbdb)             | Aircraft data.                                                                                                              |
| &nbsp;&nbsp;↳ [airport-data][apdata] | (via adsbdb)             | Aircraft photographs.                                                                                                       |
| &nbsp;&nbsp;↳ Flight routes          | (via adsbdb)             | The work of David Taylor (Edinburgh) and Jim Mason (Glasgow). **May not be copied, published, or incorporated into other databases without the explicit permission of David J Taylor, Edinburgh.** |

If you fork Squawk for anything beyond personal hobby use, please contact
the upstream maintainers before scaling traffic or persisting their data.

[tar1090-db]: https://github.com/wiedehopf/tar1090-db
[adsbdb]:     https://www.adsbdb.com/
[planebase]:  https://planebase.biz/
[apdata]:     https://airport-data.com/

## Licence

GPL-3.0 — see [LICENSE](LICENSE).
