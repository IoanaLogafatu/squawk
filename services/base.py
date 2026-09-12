"""
services/base.py

BaseService — an always-on process that keeps a shared state file current.

A "service" in Squawk is work several modules would otherwise each do for
themselves: look up the same thing, simulate the same flight. Instead one
process does it on its own schedule and writes the answer to

    <data_dir>/services/<name>/state.json

and every module that needs it reads that file. Consumers never call the
service process — they read whatever is currently on disk, and if the
service is not running they simply find nothing, or nothing new.

The service is the file's only writer, so readers need no lock: every
write is an atomic rename, and a reader sees either the previous state or
the next one, never half of either. "Only writer" is enforced rather than
assumed — a running service holds an exclusive flock on service.lock for
its whole life, and a second copy started by mistake refuses to run
instead of quietly flapping the file between two sets of answers.

Run one with `python main.py service <name>`; the names available are the
SERVICES registry in services/__init__.py.

Settings come from an optional [service.<name>] block in config.toml. Every
service accepts debug_level; one with settings of its own declares a
ServiceConfig subclass as its config_class, and the block is validated
against that.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import time
from abc import ABC, abstractmethod
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

from config import ServiceConfig

if TYPE_CHECKING:
    from config import Config

STATE_FILE = "state.json"
LOCK_FILE  = "service.lock"


class ServiceAlreadyRunning(Exception):
    """Raised when a second copy of a service is started."""


class BaseService(ABC):
    """
    Args:
        name:    the service's name as listed in config, e.g. "concorde". Also
                 its logger name and the folder its state lives in.
        service: this service's settings, from its [service.<name>] block.
        app:     whole-installation config (data_dir, observer position).
    """

    # The dataclass this service's [service.<name>] block is read into.
    config_class: type[ServiceConfig] = ServiceConfig

    # How often run() calls refresh(). A subclass sets its own.
    refresh_interval_seconds: float = 1.0

    def __init__(self, name: str, service: ServiceConfig, app: "Config") -> None:
        self.name       = name
        self.service    = service
        self.app        = app
        self.dir        = service_dir(app.data_dir, name)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.dir / STATE_FILE
        self.lock_path  = self.dir / LOCK_FILE
        self.log        = logging.getLogger(name)

    # -- the work -----------------------------------------------------------

    @abstractmethod
    def refresh(self) -> dict[str, Any] | None:
        """
        Work out the current state.

        Return the new state to publish, or None when there is nothing new
        — the file is then left as it is.
        """
        raise NotImplementedError

    # -- the process --------------------------------------------------------

    def run(self) -> None:
        """
        Refresh and publish forever, every refresh_interval_seconds.

        Raises ServiceAlreadyRunning if another process is already this
        service.
        """
        with self._sole_writer():
            self.log.info("service '%s' started (refreshing every %.1fs, writing %s)",
                          self.name, self.refresh_interval_seconds, self.state_path)
            while True:
                started = time.monotonic()
                state   = self.refresh()
                if state is not None:
                    self.write_state(state)
                elapsed = time.monotonic() - started
                time.sleep(max(0.0, self.refresh_interval_seconds - elapsed))

    def write_state(self, state: dict[str, Any]) -> None:
        """Replace the state file atomically — readers never see a partial write."""
        tmp = self.state_path.with_suffix(".json.tmp")
        with open(tmp, "w") as handle:
            json.dump(state, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.state_path)

    @contextmanager
    def _sole_writer(self) -> Iterator[None]:
        """
        Hold this service's lock for as long as the block runs.

        The lock is a separate file from the state because state is written
        by atomic rename, which replaces the inode — a lock on the state file
        itself would vanish with the first write.
        """
        with open(self.lock_path, "w") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ServiceAlreadyRunning(
                    f"service '{self.name}' is already running — "
                    f"{self.lock_path} is held by another process"
                ) from None
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# The consumer's side — reading, never calling
# ---------------------------------------------------------------------------

def service_dir(data_dir: Path | str, name: str) -> Path:
    return Path(data_dir) / "services" / name


def read_state(data_dir: Path | str, name: str) -> dict[str, Any] | None:
    """
    A service's current state, as last published, or None if there is none.

    None covers both "the service has never run" and "the file cannot be
    read": either way the consumer has no answer to work with, and the
    service's next write replaces whatever is there.
    """
    path = service_dir(data_dir, name) / STATE_FILE
    try:
        with open(path) as handle:
            state = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return state if isinstance(state, dict) else None


def is_running(data_dir: Path | str, name: str) -> bool:
    """Whether some process currently holds the service's lock."""
    lock_path = service_dir(data_dir, name) / LOCK_FILE
    if not lock_path.exists():
        return False
    with open(lock_path, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False
