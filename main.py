"""
main.py

Squawk's single entrypoint.

    python main.py <chain_family> <name>    one chain, as one process
    python main.py service <name>           one service, as one process
    python main.py run <profile>            a [run.<profile>] set of them

Every Squawk process is one chain or one service, started this way. There
is no start-up order: each process creates whatever it needs under
data_dir and gets on with it. The base build is six of them —

    python main.py service        concorde
    python main.py ingest_chain   concorde_A
    python main.py ingest_chain   concorde_B
    python main.py snapshot_chain snapshot_chain
    python main.py output_chain   display
    python main.py output_chain   history

— or all six at once with `python main.py run concorde_test`, which starts
each of them as its own process and stops them all if any one exits. See
chains/launcher.py.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from pathlib import Path

from chains import ingest_chain, launcher, output_chain, snapshot_chain
from config import CHAIN_FAMILIES, Config, ConfigError, load_config
from services import SERVICES
from services.base import ServiceAlreadyRunning

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
        if args.family == "run":
            profile = app.profile(args.name)
        elif args.family == "service":
            service = app.service(args.name)
        else:
            chain = app.chain(args.family, args.name)
    except ConfigError as exc:
        print(f"squawk: {exc}", file=sys.stderr)
        return 2

    _prepare_data_dir(app)

    if args.family == "run":
        # The launcher is not a module and has no config block; it logs
        # what it starts and stops, which is its whole job.
        _setup_logging("info")
        return launcher.run(profile, app, args.config)

    _stop_on_signal()

    if args.family == "service":
        _setup_logging(service.debug_level)
        log = logging.getLogger(args.name)
        try:
            SERVICES[args.name](args.name, service, app).run()
        except ServiceAlreadyRunning as exc:
            log.error("%s", exc)
            return 1
        except KeyboardInterrupt:
            log.info("stopped")
        return 0

    _setup_logging(chain.debug_level)
    log = logging.getLogger(chain.name)
    try:
        RUNNERS[args.family](chain, app)
    except KeyboardInterrupt:
        log.info("stopped")
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="squawk",
        description="Run one Squawk chain or service as a process, or a run profile of them.",
    )
    parser.add_argument(
        "family", choices=(*CHAIN_FAMILIES, "service", "run"),
        help="a chain family, 'service', or 'run' for a profile",
    )
    parser.add_argument(
        "name",
        help="the config block's key, e.g. concorde_A (for the snapshot chain: "
             "snapshot_chain); a service name; or a [run.<name>] profile",
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


def _stop_on_signal() -> None:
    """
    Stop cleanly on SIGINT or SIGTERM, once.

    SIGTERM is how a run profile's launcher stops its processes, so it gets
    the same clean exit as Ctrl-C. Under a launcher a Ctrl-C reaches a
    process twice — from the terminal and forwarded by the launcher — so
    after the first signal further ones are ignored rather than
    interrupting the shutdown they are asking for.
    """
    def stop(_signum: int, _frame: object) -> None:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)


def _prepare_data_dir(app: Config) -> None:
    """
    Create data_dir up front so a chain's first write is never the thing
    that discovers the path is unwritable.
    """
    app.data_dir.mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    sys.exit(main())
