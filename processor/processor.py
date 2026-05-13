"""
processor/processor.py

Poll loop that reads the latest aircraft snapshot from storage, passes
the aircraft list through the configured filter chain, then hands off
to the configured display plugin.

The processor runs independently of ingestors — it reads whatever the
storage layer currently holds, on its own schedule.
"""

from __future__ import annotations

import time

from config import config
from display import get_display
from plugins import get_plugin
from storage import get_storage
from schemas.aircraft import aircraft_from_dict


def run() -> None:
    cfg     = config.processor
    storage = get_storage(config.storage.method, config.squawk.data_dir)
    plugin_cfgs = config.plugins
    filters = [get_plugin(name, plugin_cfgs.get(name, {})) for name in cfg.plugins]
    display = get_display(cfg.display, config.display.get(cfg.display, {})) if cfg.display else None

    while True:
        cycle_start = time.time()

        aircraft = [aircraft_from_dict(d) for d in storage.retrieve_aircraft_array()]

        for f in filters:
            aircraft = f.process(aircraft)

        if display:
            display.process(aircraft)

        sleep_for = max(0.0, cfg.poll_interval_seconds - (time.time() - cycle_start))
        time.sleep(sleep_for)
