# Squawk

**A modular aircraft tracking pipeline.**

Squawk turns a personal ADS-B receiver into a live data stream of the aircraft *you* care about. Real-time surveillance data is ingested from your own receivers, passed through a configurable chain of filter and enrichment modules, and handed off to the display of your choice — a web page, an e-paper screen, or whatever you build next. Ingestors can query external APIs, such as FlightAware tracking (for a mobile experience) or other data sources, such as weather update, even a local flying club timetable.

The whole system is built around modules. Want to track only aircraft within 20 nm? Drop in a filter. Want to look up routes from an external API? Drop in an enricher. Want to send the result to a Slack channel or a handheld display? Write a display module. The processor doesn't care what each step does, only that it accepts `list[Aircraft]` and returns `list[Aircraft]`.

## Pipeline

```
[ Ingestors ] → [ Storage ] → [ Processors (Independent Chains) ] → [ Displays ]
```

- **Ingestors** poll external sources (your tar1090 receiver, the bundled Concorde simulator, anything you write) and write `Aircraft` records to storage each cycle.
- **Storage** persists aircraft records through a pluggable storage backend.
- **Processors** run independent chains on their own schedules (e.g. Pushover alerts, e-paper display), running their own module chains and handing results to their respective displays.
- **Modules** filter or enrich. They all share one interface: `list[Aircraft] → list[Aircraft]`.

Each ingestor and processor chain runs on its own background thread, so pipelines with different poll intervals or filter criteria don't block each other.

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

[processors.screen]
enabled               = true
poll_interval_seconds = 5
modules               = ["tar1090_db", "closest_filter"]
display               = "http"

[processors.pushover]
enabled               = true
poll_interval_seconds = 5
modules               = ["ground_distance_filter", "tar1090_db", "registration_filter", "adsbdb"]
display               = "pushover"


[display.http]
port = 7700
```

This is a partial excerpt — a real config also needs `[squawk]`, `[storage]`, a `[modules.<name>]` block for every name listed above (empty if it takes no options), and a `[display.http.panels.screen]` block for the `screen` chain. A block referenced by name that doesn't exist fails at startup rather than silently defaulting. See [`config.toml.example`](config.toml.example) for a complete, loadable reference.

`config.toml` itself is gitignored. `config.toml.example` is the reference kept in version control — keep it in sync when adding new keys.


## Writing your own module

```python
# modules/altitude_floor.py
from modules import BaseModule
from schemas.aircraft import Aircraft

class AltitudeFloor(BaseModule):
    def __init__(self, min_feet: int) -> None:
        self._min_feet = min_feet

    def process(self, aircraft: list[Aircraft]) -> list[Aircraft]:
        return [a for a in aircraft if (a.dynamic.altitude_feet or 0) >= self._min_feet]

def get(cfg: dict) -> AltitudeFloor:
    return AltitudeFloor(min_feet=cfg.get("min_feet", 5000))
```

Wire it into the chain:

```toml
[processors.watchlist]
enabled = true
modules = ["altitude_floor", "closest_filter"]
display = "console"

[modules.altitude_floor]
min_feet = 10000

[modules.closest_filter]
[display.console]
```

The module is discovered by name — no registration step. Every module and display named above needs its own block, even if empty — a chain referencing a block that doesn't exist fails at startup rather than silently skipping it. See [`docs/modules-guide.md`](docs/modules-guide.md) for the full contract.

## Tests

```bash
./runtests.sh
```

## Documentation

- [`docs/modules-guide.md`](docs/modules-guide.md) — writing filter and enrichment modules
- [`docs/display-guide.md`](docs/display-guide.md) — writing display modules
- [`docs/storage-guide.md`](docs/storage-guide.md) — writing a new storage backend
- [`docs/primary_ingestor.md`](docs/primary_ingestor.md) — design notes on `personal_adsb`

## Acknowledgements

Squawk is a hobby project that leans heavily on free public data sources.
Please honour their terms.

| Source                               | Used by                  | Notes                                                                                                                       |
| ------------------------------------ | ------------------------ | --------------------------------------------------------------------------------------------------------------------------- |
| [wiedehopf/tar1090-db][tar1090-db]   | `tar1090_db` module      | Aircraft registration and type database.                                                                                    |
| [adsbdb.com][adsbdb]                 | `adsbdb` module          | API that aggregates the three sources below.                                                                                |
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
