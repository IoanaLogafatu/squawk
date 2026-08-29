"""
processor/processor.py

Poll loop that reads the latest aircraft snapshot from storage, passes
the aircraft list through the configured filter chain, then hands off
to the configured display module.

The processor runs independently of ingestors — it reads whatever the
storage layer currently holds, on its own schedule.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import time

from config import config
from display import get_display
from modules import get_module
from storage import get_storage
from schemas.aircraft import aircraft_from_dict

if TYPE_CHECKING:
    from config import ProcessorConfig


def run(proc_cfg: ProcessorConfig | None = None) -> None:
    cfg = proc_cfg or config.processor
    if cfg is None:
        print("No processor configuration provided.")
        return

    storage = get_storage(config.storage.method, config.squawk.data_dir)
    module_cfgs = config.modules
    filters = [get_module(name, module_cfgs.get(name, {})) for name in cfg.modules]
    display = get_display(cfg.display, config.display.get(cfg.display, {})) if cfg.display else None

    print(f"Processor chain [{cfg.name}] started: {len(filters)} modules -> display: {cfg.display or 'none'}")

    while True:
        cycle_start = time.time()

        try:
            aircraft = [aircraft_from_dict(d) for d in storage.retrieve_aircraft_array()]

            for f in filters:
                aircraft = f.process(aircraft)

            if display:
                display.process(aircraft)
        except Exception as e:
            print(f"Error in processor chain [{cfg.name}]: {e}")

        sleep_for = max(0.0, cfg.poll_interval_seconds - (time.time() - cycle_start))
        time.sleep(sleep_for)

