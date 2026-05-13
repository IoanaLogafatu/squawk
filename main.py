"""
main.py

Entry point for Squawk.

Reads config and starts each enabled ingestor in its own thread.
Each ingestor runs its own poll loop independently — different sources
can have different poll intervals without blocking each other.

Usage:
    python main.py

Ingestors run until KeyboardInterrupt (Ctrl+C), at which point all
threads are signalled to stop and the process exits cleanly.

Adding a new ingestor:
    1. Implement the ingestor in ingestor/<name>/ingestor.py with a run() function
    2. Add a config section in config.toml
    The ingestor will be discovered and started automatically.
"""

from __future__ import annotations

import importlib
import threading

from config import config


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    threads: list[threading.Thread] = []

    for name, cfg in config.ingestors.items():
        if not cfg.get("enabled", False):
            continue
        module = importlib.import_module(f"ingestor.{name}.ingestor")
        thread = threading.Thread(target=module.run, name=name, daemon=True)
        thread.start()
        threads.append(thread)

    if config.processor:
        from processor.processor import run as processor_run
        thread = threading.Thread(target=processor_run, name="Processor", daemon=True)
        thread.start()
        threads.append(thread)

    if not threads:
        print("Nothing enabled in config.toml — nothing to do.")
        return

    # Keep the main thread alive until Ctrl+C
    try:
        for thread in threads:
            thread.join()
    except KeyboardInterrupt:
        print("\nSquawk stopped.")


if __name__ == "__main__":
    main()
