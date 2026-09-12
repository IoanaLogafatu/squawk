"""
chains/launcher.py

Runner for `python main.py run <profile>` — start a [run.<name>] profile.

A profile is only a launcher convenience. Each chain it lists is still its
own OS process, started exactly as it would be by hand
(`python main.py <family> <name>`), and runs exactly as it would by hand.
What the launcher adds is treating them as one unit:

  - Every service in the installation's `services` list is started too,
    before the chains, since the chains depend on what services publish.
    A service already running elsewhere — say, under another profile — is
    left to that process rather than started twice.
  - If any one process exits, for any reason, every other one is stopped.
    A profile is meant to be watched as a whole, not left half alive with
    one member silently dead.
  - Ctrl-C (or SIGTERM) on the launcher is forwarded to every process, so
    the whole profile stops together.

All the processes share the launcher's terminal, so their output
interleaves on one screen.
"""

from __future__ import annotations

import logging
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from config import Config, RunProfile
from services.base import is_running

MAIN_PY = Path(__file__).resolve().parent.parent / "main.py"

# How often the launcher checks whether a process has exited.
WATCH_INTERVAL_SECONDS = 0.2

# How long processes get to stop cleanly before they are killed outright.
STOP_GRACE_SECONDS = 10.0


@dataclass
class _Child:
    label:   str
    process: subprocess.Popen


class _Stop(Exception):
    def __init__(self, signum: int) -> None:
        self.signum = signum


def run(profile: RunProfile, app: Config, config_path: Path | None) -> int:
    """
    Launch a profile and supervise it until it stops.

    Returns the launcher's exit status: 0 if it was asked to stop, non-zero
    if it stopped because one of its processes exited.
    """
    log      = logging.getLogger(f"run.{profile.name}")
    commands = processes_for(profile, app, log)

    def on_signal(signum: int, _frame: object) -> None:
        raise _Stop(signum)

    previous = {sig: signal.signal(sig, on_signal) for sig in (signal.SIGINT, signal.SIGTERM)}

    children: list[_Child] = []
    forward  = signal.SIGTERM
    status   = 0
    try:
        for family, name in commands:
            children.append(_spawn(family, name, config_path))
        log.info("profile '%s' started: %s", profile.name,
                 ", ".join(child.label for child in children))

        exited = _wait_for_first_exit(children)
        log.error("%s exited with status %s — stopping the rest of profile '%s'",
                  exited.label, exited.process.returncode, profile.name)
        status = exited.process.returncode or 1

    except _Stop as stop:
        log.info("received %s — stopping profile '%s'",
                 signal.Signals(stop.signum).name, profile.name)
        forward = signal.Signals(stop.signum)

    finally:
        # A second Ctrl-C while stopping must not abandon the children.
        for sig in previous:
            signal.signal(sig, signal.SIG_IGN)
        _stop_all(children, forward, log)
        for sig, handler in previous.items():
            signal.signal(sig, handler)

    return status


def processes_for(profile: RunProfile, app: Config, log: logging.Logger) -> list[tuple[str, str]]:
    """What to start, as (family, name): services first, then the profile's chains."""
    commands: list[tuple[str, str]] = []
    for service in app.services:
        if is_running(app.data_dir, service):
            log.info("service '%s' is already running — using that one", service)
        else:
            commands.append(("service", service))
    return commands + list(profile.chains)


def _spawn(family: str, name: str, config_path: Path | None) -> _Child:
    command = [sys.executable, str(MAIN_PY), family, name]
    if config_path is not None:
        command += ["--config", str(Path(config_path).resolve())]
    return _Child(label=f"{family}.{name}", process=subprocess.Popen(command))


def _wait_for_first_exit(children: list[_Child]) -> _Child:
    while True:
        for child in children:
            if child.process.poll() is not None:
                return child
        time.sleep(WATCH_INTERVAL_SECONDS)


def _stop_all(children: list[_Child], sig: int, log: logging.Logger) -> None:
    """Signal every live child, give them a grace period, then kill stragglers."""
    for child in children:
        if child.process.poll() is None:
            child.process.send_signal(sig)

    deadline = time.monotonic() + STOP_GRACE_SECONDS
    for child in children:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            child.process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            log.warning("%s did not stop within %.0fs — killing it",
                        child.label, STOP_GRACE_SECONDS)
            child.process.kill()
            child.process.wait()
