"""
tests/test_system.py

Tests for the system state module.

Covers:
  1. set/get round-trip and absent-key defaults
  2. snapshot() returns a copy, not the live dict
  3. Concurrent writers leave the dict consistent
"""

from __future__ import annotations

import threading

import pytest

import system


@pytest.fixture(autouse=True)
def _clean_state():
    system.clear()
    yield
    system.clear()


# ===========================================================================
# 1. set / get
# ===========================================================================

def test_set_then_get_round_trip():
    system.set("tracked", 145)
    assert system.get("tracked") == 145


def test_set_overwrites_previous_value():
    system.set("tracked", 1)
    system.set("tracked", 2)
    assert system.get("tracked") == 2


def test_get_absent_key_returns_none():
    assert system.get("never_published") is None


def test_get_absent_key_returns_default():
    assert system.get("never_published", 0) == 0


def test_get_present_key_ignores_default():
    system.set("tracked", 7)
    assert system.get("tracked", 999) == 7


def test_clear_removes_published_keys():
    system.set("tracked", 3)
    system.clear()
    assert system.get("tracked") is None
    assert system.snapshot() == {}


# ===========================================================================
# 2. snapshot
# ===========================================================================

def test_snapshot_contains_published_keys():
    system.set("tracked", 42)
    assert system.snapshot() == {"tracked": 42}


def test_snapshot_of_empty_state_is_empty_dict():
    assert system.snapshot() == {}


def test_snapshot_is_a_copy_not_the_live_dict():
    system.set("tracked", 5)
    snap = system.snapshot()
    snap["tracked"] = 9999
    snap["injected"] = "nope"

    assert system.get("tracked") == 5
    assert system.get("injected") is None
    assert system.snapshot() == {"tracked": 5}


def test_snapshot_does_not_see_later_writes():
    system.set("tracked", 1)
    snap = system.snapshot()
    system.set("tracked", 2)
    assert snap["tracked"] == 1


# ===========================================================================
# 3. Concurrency
# ===========================================================================

def test_concurrent_writers_leave_state_consistent():
    # Each thread owns its own key, so every key must survive with its own value.
    threads = []
    for i in range(16):
        def write(n=i):
            for _ in range(200):
                system.set(f"key_{n}", n)
        threads.append(threading.Thread(target=write))

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    snap = system.snapshot()
    assert len(snap) == 16
    for i in range(16):
        assert snap[f"key_{i}"] == i


def test_concurrent_writers_to_one_key_leave_a_published_value():
    # Last writer wins; the requirement is that the dict is never left corrupt
    # and the surviving value is one that was actually published.
    written = set(range(32))
    threads = [
        threading.Thread(target=lambda n=n: [system.set("tracked", n) for _ in range(100)])
        for n in written
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert system.get("tracked") in written


def test_snapshot_during_concurrent_writes_is_readable():
    stop = threading.Event()

    def writer():
        n = 0
        while not stop.is_set():
            system.set("tracked", n)
            n += 1

    t = threading.Thread(target=writer)
    t.start()
    try:
        for _ in range(500):
            snap = system.snapshot()
            assert isinstance(snap, dict)
            if "tracked" in snap:
                assert isinstance(snap["tracked"], int)
    finally:
        stop.set()
        t.join()
