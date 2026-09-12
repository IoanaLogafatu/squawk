"""
services/base.py

BaseService — a service's own state, in a file, under a lock.

A "service" in Squawk is something several modules need a shared answer
from. In this build the only one is the Concorde simulator, and it is not
a running process at all: it is a state file plus the pure functions that
derive an answer from it. Any number of processes can ask, none of them
has to have started first, and they all get the same answer.

That only holds if read-modify-write is atomic across processes, which is
what this class provides. `locked()` takes an exclusive flock on a
sidecar lock file for the whole read-decide-write sequence — not just the
write — because the decisions services make ("is a flight already in
progress?") are exactly the ones two processes must not make
independently.

The lock is a separate file from the state because state is written by
atomic rename, which replaces the inode: a lock held on the state file
itself would be released into thin air by the very write it was guarding.

A future service that really does talk to the outside world (weather,
FlightAware) still fits: it becomes a process that refreshes this same
local state file, and the modules that consume it carry on reading the
file and never call the process.

State lives at <data_dir>/services/<name>/state.json.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class BaseService:

    def __init__(self, name: str, data_dir: Path) -> None:
        self.name = name
        self.dir  = Path(data_dir) / "services" / name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.dir / "state.json"
        self.lock_path  = self.dir / "state.lock"
        self.log = logging.getLogger(f"service.{name}")

    # -- locking ------------------------------------------------------------

    @contextmanager
    def locked(self) -> Iterator[None]:
        """
        Hold this service's exclusive lock for the body of the block.

        Wrap the whole read-decide-write sequence in it, never just the
        write.
        """
        with open(self.lock_path, "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    # -- state --------------------------------------------------------------

    def read_state(self) -> dict[str, Any] | None:
        """
        Current state, or None if there is none yet.

        A corrupt or truncated file is also reported as None: the caller's
        answer to "no state" is to build fresh state, which is the right
        recovery from an unreadable file too.
        """
        try:
            with open(self.state_path) as handle:
                return json.load(handle)
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, OSError) as exc:
            self.log.warning("unreadable state file %s (%s) — starting fresh",
                             self.state_path, exc)
            return None

    def write_state(self, state: dict[str, Any]) -> None:
        """Replace the state file atomically — readers never see a partial write."""
        tmp = self.state_path.with_suffix(".json.tmp")
        with open(tmp, "w") as handle:
            json.dump(state, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.state_path)
