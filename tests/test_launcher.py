"""
tests/test_launcher.py

Run profiles: one command, many processes, one unit.

The supervision tests use real child processes — stand-ins that sleep or
exit on cue rather than real chains, so a test controls exactly who dies
and when — because what is being tested is signals and exit statuses
between processes.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time

from chains import launcher
from config import RunProfile
from services.concorde.service import ConcordeService

LOG = logging.getLogger("test")

PROFILE = RunProfile(name="test", chains=[
    ("ingest_chain", "concorde_A"),
    ("output_chain", "display"),
])


def test_a_profile_starts_its_services_then_its_chains(app):
    assert launcher.processes_for(PROFILE, app, LOG) == [
        ("service", "concorde"),
        ("ingest_chain", "concorde_A"),
        ("output_chain", "display"),
    ]


def test_a_service_already_running_is_not_started_twice(app):
    with ConcordeService("concorde", app.service("concorde"), app)._sole_writer():
        commands = launcher.processes_for(PROFILE, app, LOG)

    assert ("service", "concorde") not in commands


def _fake_spawn(scripts: dict[str, str], started: list):
    """Replace each process with a small Python script, keyed by label."""
    def spawn(family, name, _config_path):
        label   = f"{family}.{name}"
        process = subprocess.Popen([sys.executable, "-c", scripts[label]])
        child   = launcher._Child(label=label, process=process)
        started.append(child)
        return child
    return spawn


def test_when_one_process_dies_the_rest_are_stopped(app, monkeypatch):
    started: list = []
    forever = "import time\nwhile True: time.sleep(0.1)"
    monkeypatch.setattr(launcher, "_spawn", _fake_spawn({
        "service.concorde":        forever,
        "ingest_chain.concorde_A": "import time; time.sleep(0.5); raise SystemExit(3)",
        "output_chain.display":    forever,
    }, started))

    began  = time.monotonic()
    status = launcher.run(PROFILE, app, None)

    assert status == 3
    assert all(child.process.poll() is not None for child in started)
    assert time.monotonic() - began < launcher.STOP_GRACE_SECONDS, \
        "survivors should stop on the signal, not be waited out"


def test_a_process_exiting_cleanly_still_stops_the_profile(app, monkeypatch):
    started: list = []
    forever = "import time\nwhile True: time.sleep(0.1)"
    monkeypatch.setattr(launcher, "_spawn", _fake_spawn({
        "service.concorde":        forever,
        "ingest_chain.concorde_A": forever,
        "output_chain.display":    "raise SystemExit(0)",
    }, started))

    status = launcher.run(PROFILE, app, None)

    assert status != 0
    assert all(child.process.poll() is not None for child in started)


def test_a_process_ignoring_the_stop_signal_is_killed(app, monkeypatch):
    started: list = []
    stubborn = ("import signal, time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "while True: time.sleep(0.1)")
    monkeypatch.setattr(launcher, "STOP_GRACE_SECONDS", 1.0)
    monkeypatch.setattr(launcher, "_spawn", _fake_spawn({
        "service.concorde":        stubborn,
        "ingest_chain.concorde_A": "raise SystemExit(1)",
        "output_chain.display":    stubborn,
    }, started))

    launcher.run(PROFILE, app, None)

    assert all(child.process.poll() is not None for child in started)
