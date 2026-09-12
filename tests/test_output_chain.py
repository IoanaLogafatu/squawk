"""
tests/test_output_chain.py

An output chain's single input, decided by its source.

A live chain only ever calls send(); a deletions chain only ever calls
on_deleted(), once per expired aircraft, and never reads the live picture.
These use a recording output module so the tests can see exactly which
hook was called with what.
"""

from __future__ import annotations

import logging

from chains.output_chain import CursorFile, poll_deletions, poll_live
from output.base import BaseOutput
from transforms import build_transforms
from transforms.base import BaseTransform


class Recorder(BaseOutput):
    def __init__(self, name, chain, app):
        super().__init__(name, chain, app)
        self.sent:    list[list[str]] = []
        self.deleted: list[list[str]] = []

    def send(self, aircraft):
        self.sent.append([a.meta.icao_hex for a in aircraft])

    def on_deleted(self, aircraft):
        self.deleted.append([a.meta.icao_hex for a in aircraft])


class DropHex(BaseTransform):
    """Filters one aircraft out — proves a chain's transforms apply to its input."""

    def process(self, aircraft):
        return [a for a in aircraft if a.meta.icao_hex != "BBBBBB"]


def _expire(snap, make_aircraft, *hexes):
    snap.chain.expiry_minutes = 5
    for hex_code in hexes:
        snap.save([make_aircraft(hex_code=hex_code, age_seconds=600)])
    snap.expire()


def _deletions_chain(app):
    chain = app.output_chains["history"]
    return Recorder(chain.type, chain, app), CursorFile(app.data_dir, chain.name)


LOG = logging.getLogger("test")


def test_a_live_chain_sends_the_picture_and_nothing_else(app, snap, make_aircraft):
    chain  = app.output_chains["display"]
    output = Recorder(chain.type, chain, app)
    snap.save([make_aircraft(hex_code="AAAAAA")])
    _expire(snap, make_aircraft, "CCCCCC")

    poll_live(snap, build_transforms(chain, app), output)

    assert output.sent == [["AAAAAA"]]
    assert output.deleted == []


def test_a_deletions_chain_reports_each_aircraft_separately(app, snap, make_aircraft):
    output, cursor = _deletions_chain(app)
    _expire(snap, make_aircraft, "AAAAAA", "BBBBBB")

    poll_deletions(snap, [], output, cursor, LOG)

    assert output.deleted == [["AAAAAA"], ["BBBBBB"]]
    assert output.sent == []


def test_a_deletions_chain_never_sends_the_live_picture(app, snap, make_aircraft):
    output, cursor = _deletions_chain(app)
    snap.save([make_aircraft(hex_code="AAAAAA")])   # live, not expired

    poll_deletions(snap, [], output, cursor, LOG)

    assert output.sent == []
    assert output.deleted == []


def test_a_deletion_is_reported_once(app, snap, make_aircraft):
    output, cursor = _deletions_chain(app)
    _expire(snap, make_aircraft, "AAAAAA")

    poll_deletions(snap, [], output, cursor, LOG)
    poll_deletions(snap, [], output, cursor, LOG)

    assert output.deleted == [["AAAAAA"]]


def test_a_restarted_deletions_chain_resumes_rather_than_replays(app, snap, make_aircraft):
    output, cursor = _deletions_chain(app)
    _expire(snap, make_aircraft, "AAAAAA")
    poll_deletions(snap, [], output, cursor, LOG)

    _expire(snap, make_aircraft, "BBBBBB")
    restarted, fresh_cursor = _deletions_chain(app)   # new objects, same files
    poll_deletions(snap, [], restarted, fresh_cursor, LOG)

    assert restarted.deleted == [["BBBBBB"]]


def test_a_deletions_chain_runs_its_transforms(app, snap, make_aircraft):
    output, cursor = _deletions_chain(app)
    _expire(snap, make_aircraft, "AAAAAA", "BBBBBB")
    drop = DropHex("drop", app.output_chains["history"], app)

    poll_deletions(snap, [drop], output, cursor, LOG)

    assert output.deleted == [["AAAAAA"]]
