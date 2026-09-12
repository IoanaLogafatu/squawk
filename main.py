"""
main.py

Squawk's single entrypoint.

    python main.py <chain_family> <name>

Every Squawk process is one chain, started this way. There is no
supervisor, no daemon and no start-up order: each process creates
whatever it needs under data_dir and gets on with it. The base build is
five of them —

    python main.py ingest_chain   concorde_A
    python main.py ingest_chain   concorde_B
    python main.py snapshot_chain snapshot_chain
    python main.py output_chain   display
    python main.py output_chain   history

— and there is no sixth for the Concorde service, because a service is a
shared file, not a process. See services/base.py.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from chains import ingest_chain, output_chain, snapshot_chain
from config import CHAIN_FAMILIES, Config, ConfigError, load_config

# Squawk's debug_level names, mapped onto stdlib logging's.
LOG_LEVELS = {
    "error": logging.ERROR,
    "warn":  logging.WARNING,
    "info":  logging.INFO,
    "debug": logging.DEBUG,
}

RUNNERS = {
    "ingest_chain":   ingest_chain.run,
    "snapshot_chain": snapshot_chain.run,
    "output_chain":   output_chain.run,
}


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    try:
        app = load_config(args.config)
    except ConfigError as exc:
        print(f"squawk: {exc}", file=sys.stderr)
        return 2

    try:
        chain = app.chain(args.chain_family, args.name)
    except ConfigError as exc:
        print(f"squawk: {exc}", file=sys.stderr)
        return 2

    _setup_logging(chain.debug_level)
    _prepare_data_dir(app)

    log = logging.getLogger(chain.name)
    try:
        RUNNERS[args.chain_family](chain, app)
    except KeyboardInterrupt:
        log.info("stopped")
        return 0
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="squawk",
        description="Run one Squawk chain as a process.",
    )
    parser.add_argument(
        "chain_family", choices=CHAIN_FAMILIES,
        help="which family of chain to run",
    )
    parser.add_argument(
        "name",
        help="the config block's key, e.g. concorde_A (for the snapshot chain: snapshot_chain)",
    )
    parser.add_argument(
        "--config", type=Path, default=None,
        help="path to config.toml (default: alongside main.py)",
    )
    return parser.parse_args(argv)


def _setup_logging(debug_level: str) -> None:
    """
    One handler, on stderr, at this chain's level.

    stderr specifically: the console output module writes the picture to
    stdout, and keeping logs off that stream means `python main.py
    output_chain display > picture.txt` gives you the picture and leaves
    the logs on screen.
    """
    logging.basicConfig(
        level  = LOG_LEVELS[debug_level],
        format = "%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt= "%H:%M:%S",
        stream = sys.stderr,
    )


def _prepare_data_dir(app: Config) -> None:
    """
    Create data_dir up front so a chain's first write is never the thing
    that discovers the path is unwritable.
    """
    app.data_dir.mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    sys.exit(main())
