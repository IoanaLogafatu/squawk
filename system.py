"""
system.py

Installation-level facts, published by whichever component owns them and read
by displays at render time.

Displays read system state. Filters do not — a filter that branches on system
state is no longer reproducible from its config block, which is the property
the module architecture exists to protect.

Values are whatever the publisher last set. There is no history, no expiry and
no schema; a key that has never been published is simply absent.

Keys currently published:
    tracked — int, aircraft currently in storage (published by the backend
              after each save, once stale records have been expired).
"""

from __future__ import annotations

import threading


_STATE: dict[str, object] = {}
_LOCK = threading.Lock()


def set(key: str, value) -> None:
    """Publish a fact. Overwrites whatever was there."""
    with _LOCK:
        _STATE[key] = value


def get(key: str, default=None):
    """Read a published fact, or `default` if nothing has published it."""
    with _LOCK:
        return _STATE.get(key, default)


def snapshot() -> dict:
    """A copy of everything published so far — safe to serialise or mutate."""
    with _LOCK:
        return dict(_STATE)


def clear() -> None:
    """Drop all published state. For tests only."""
    with _LOCK:
        _STATE.clear()
