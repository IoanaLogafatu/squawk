"""
tests/test_snapshot_expiry.py

Expiry, and telling the output chains about it.

Expiry is the only part of the snapshot with a process of its own, and
the only mechanism by which an aircraft ever leaves the picture — ingest
chains only ever add. The deletion notices are how an output chain finds
out, and the cursor logic is what makes "every chain sees every deletion
exactly once" true regardless of how fast each one polls.
"""

from __future__ import annotations

from schemas.aircraft import Location


def test_a_stale_aircraft_is_deleted(snap, make_aircraft):
    snap.chain.expiry_minutes = 5
    snap.save([make_aircraft(hex_code="400F6A", age_seconds=600)])

    deleted = snap.expire()

    assert [a.meta.icao_hex for a in deleted] == ["400F6A"]
    assert snap.read_all() == []


def test_a_fresh_aircraft_is_left_alone(snap, make_aircraft):
    snap.chain.expiry_minutes = 5
    snap.save([make_aircraft(age_seconds=10)])

    assert snap.expire() == []
    assert len(snap.read_all()) == 1


def test_expiry_only_takes_the_stale_ones(snap, make_aircraft):
    snap.chain.expiry_minutes = 5
    snap.save([make_aircraft(hex_code="400F6A", age_seconds=600)])
    snap.save([make_aircraft(hex_code="AAAAAA", age_seconds=10)])

    deleted = snap.expire()

    assert [a.meta.icao_hex for a in deleted] == ["400F6A"]
    assert [a.meta.icao_hex for a in snap.read_all()] == ["AAAAAA"]


def test_a_deleted_aircraft_leaves_a_notice_carrying_its_last_state(snap, make_aircraft):
    snap.chain.expiry_minutes = 5
    departing = make_aircraft(age_seconds=600)
    departing.location = Location(latitude=52.5, altitude_feet=9000)
    snap.save([departing])

    snap.expire()

    notices, _ = snap.read_deletions_since(None)
    assert len(notices) == 1
    assert notices[0].meta.icao_hex == "400F6A"
    assert notices[0].location.altitude_feet == 9000


def test_each_reader_sees_a_deletion_once(snap, make_aircraft):
    snap.chain.expiry_minutes = 5
    snap.save([make_aircraft(age_seconds=600)])
    snap.expire()

    first, cursor = snap.read_deletions_since(None)
    again, cursor = snap.read_deletions_since(cursor)

    assert len(first) == 1
    assert again == []


def test_two_readers_each_see_every_deletion(snap, make_aircraft):
    """
    Two output chains, independent cursors.

    The second chain is behind — it has not polled yet — and must still
    receive both deletions when it does, not just the one that happened
    after it started.
    """
    snap.chain.expiry_minutes = 5

    snap.save([make_aircraft(hex_code="400F6A", age_seconds=600)])
    snap.expire()
    display_seen, display_cursor = snap.read_deletions_since(None)

    snap.save([make_aircraft(hex_code="AAAAAA", age_seconds=600)])
    snap.expire()
    display_more, _ = snap.read_deletions_since(display_cursor)

    history_seen, _ = snap.read_deletions_since(None)

    assert [a.meta.icao_hex for a in display_seen] == ["400F6A"]
    assert [a.meta.icao_hex for a in display_more] == ["AAAAAA"]
    assert sorted(a.meta.icao_hex for a in history_seen) == ["400F6A", "AAAAAA"]


def test_notices_arrive_in_the_order_they_happened(snap, make_aircraft):
    snap.chain.expiry_minutes = 5
    for hex_code in ("CCCCCC", "AAAAAA", "BBBBBB"):
        snap.save([make_aircraft(hex_code=hex_code, age_seconds=600)])
        snap.expire()

    notices, _ = snap.read_deletions_since(None)

    assert [a.meta.icao_hex for a in notices] == ["CCCCCC", "AAAAAA", "BBBBBB"]


def test_an_unreadable_notice_does_not_wedge_the_cursor(snap, make_aircraft):
    """A bad notice is skipped, not retried forever."""
    snap.chain.expiry_minutes = 5
    snap.save([make_aircraft(age_seconds=600)])
    snap.expire()
    (snap.deletions_dir / "20990101T000000.000000-BADBAD.json").write_text("{ not json")

    notices, cursor = snap.read_deletions_since(None)
    again, _ = snap.read_deletions_since(cursor)

    assert [a.meta.icao_hex for a in notices] == ["400F6A"]
    assert again == []


def test_old_notices_are_pruned(snap, make_aircraft):
    import os
    import time

    snap.chain.expiry_minutes = 5
    snap.chain.deletion_retention_minutes = 60
    snap.save([make_aircraft(age_seconds=600)])
    snap.expire()

    notice = next(snap.deletions_dir.glob("*.json"))
    stale  = time.time() - 2 * 60 * 60
    os.utime(notice, (stale, stale))

    assert snap.prune_deletions() == 1
    assert list(snap.deletions_dir.glob("*.json")) == []


def test_recent_notices_are_kept(snap, make_aircraft):
    snap.chain.expiry_minutes = 5
    snap.chain.deletion_retention_minutes = 60
    snap.save([make_aircraft(age_seconds=600)])
    snap.expire()

    assert snap.prune_deletions() == 0
    assert len(list(snap.deletions_dir.glob("*.json"))) == 1


def test_an_aircraft_with_no_last_seen_is_expired(snap, make_aircraft):
    """
    A record nobody can date cannot be shown to be current, and left
    alone it would sit in the picture forever.
    """
    snap.chain.expiry_minutes = 5
    undateable = make_aircraft()
    undateable.meta.last_seen = None
    snap._write_one("400F6A", undateable)

    deleted = snap.expire()

    assert [a.meta.icao_hex for a in deleted] == ["400F6A"]
