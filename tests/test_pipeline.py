"""
tests/test_pipeline.py

Every module type, wired together.

The point of the base build is that all five module types and all three
chain families work as a set, so this walks one observation the whole way
— service, ingest, transform, snapshot, transform, output — and then
walks it back out again through expiry. If a future feature is genuinely
"just add a module", this test keeps passing untouched.

It runs the service and the chains a step at a time rather than starting
their loops, so it tests the wiring without testing the sleep.
"""

from __future__ import annotations

import logging

from chains.output_chain import CursorFile, poll_deletions, poll_live
from ingest import INGESTORS
from output import OUTPUTS
from snapshot import SNAPSHOTS
from transforms import apply_transforms, build_transforms


def _build(app, output_name="display"):
    ingest_cfg = app.ingest_chains["concorde_A"]
    output_cfg = app.output_chains[output_name]

    return (
        INGESTORS[ingest_cfg.type](ingest_cfg.type, ingest_cfg, app),
        build_transforms(ingest_cfg, app),
        SNAPSHOTS[app.snapshot_chain.type](
            app.snapshot_chain.type, app.snapshot_chain, app),
        OUTPUTS[output_cfg.type](output_cfg.type, output_cfg, app),
        build_transforms(output_cfg, app),
    )


def _ingestor(app, name):
    chain = app.ingest_chains.get(name) or type(app.ingest_chains["concorde_A"])(
        name=name, type="ingest_concorde", transform=["nop"])
    app.ingest_chains[name] = chain
    return INGESTORS[chain.type](chain.type, chain, app)


def test_an_observation_travels_from_service_to_console(app, concorde, capsys):
    ingestor, ingest_transforms, snap, output, output_transforms = _build(app)

    # -- ingest chain --------------------------------------------------
    observed = ingestor.poll()
    assert len(observed) == 1
    assert observed[0].meta.icao_hex == "400F6A"
    assert list(observed[0].raw) == ["concorde_A"]
    assert observed[0].location.latitude is not None

    snap.save(apply_transforms(ingest_transforms, observed))

    # -- the snapshot is a folder of files, nothing more ---------------
    assert (app.data_dir / "snapshot" / "400F6A.json").exists()

    # -- output chain --------------------------------------------------
    poll_live(snap, output_transforms, output)

    printed = capsys.readouterr().out
    assert "400F6A" in printed
    assert "G-BOAC" in printed
    assert "LHR->JFK" in printed
    assert "SOURCE" in printed


def test_two_ingest_chains_share_one_aircraft(app, concorde, capsys):
    """
    Two chains, two processes' worth of work, one aircraft in the picture.

    They read the same Concorde service, so they must agree on identity
    and produce one file, not two — and both must show up as its sources.
    """
    snap = SNAPSHOTS[app.snapshot_chain.type](
        app.snapshot_chain.type, app.snapshot_chain, app)

    for name in ("concorde_A", "concorde_B"):
        snap.save(_ingestor(app, name).poll())

    held = snap.read_all()
    assert len(held) == 1
    assert sorted(held[0].raw) == ["concorde_A", "concorde_B"]
    assert held[0].location.latitude is not None

    _, _, _, output, _ = _build(app)
    output.send(held)
    assert "concorde_A, concorde_B" in capsys.readouterr().out


def test_the_ingestor_reports_the_services_observation_time(app, concorde):
    from datetime import datetime

    from schemas.aircraft import ensure_utc

    state    = concorde.refresh()
    concorde.write_state(state)
    observed = _ingestor(app, "concorde_A").poll()[0]

    assert observed.meta.last_seen == ensure_utc(datetime.fromisoformat(state["observed_at"]))


def test_with_no_service_running_the_ingestor_sees_nothing(app, caplog):
    ingestor = _ingestor(app, "concorde_A")

    with caplog.at_level(logging.WARNING):
        assert ingestor.poll() == []
        assert ingestor.poll() == []

    warnings = [r for r in caplog.records if "is it running" in r.message]
    assert len(warnings) == 1, "a stopped service should be reported once, not every poll"


def test_a_stale_service_state_is_not_reported_twice(app, concorde):
    """Nothing new since the last poll means nothing to say — so she can expire."""
    ingestor = _ingestor(app, "concorde_A")

    assert len(ingestor.poll()) == 1
    assert ingestor.poll() == []

    concorde.write_state(concorde.refresh())
    assert len(ingestor.poll()) == 1


def test_an_empty_snapshot_is_a_valid_picture(app, capsys):
    _, _, snap, output, transforms = _build(app)

    poll_live(snap, transforms, output)

    assert "0 aircraft" in capsys.readouterr().out


def test_expiry_reaches_a_deletions_chain(app, concorde, capsys):
    ingestor, _, snap, output, transforms = _build(app, "history")
    snap.save(ingestor.poll())

    # Expire everything, however fresh.
    snap.chain.expiry_minutes = 0
    snap.expire()

    poll_deletions(snap, transforms, output, CursorFile(app.data_dir, "history"),
                   logging.getLogger("history"))

    printed = capsys.readouterr().out
    assert "GONE" in printed
    assert "400F6A" in printed
    assert "concorde_A" in printed
    assert snap.read_all() == []


def test_transforms_run_in_every_chain_family(app, concorde, caplog):
    """
    nop earns its keep here: with debug logging on, each chain family's
    transform list must be seen to run.
    """
    for chain in (app.ingest_chains["concorde_A"],
                  app.snapshot_chain,
                  app.output_chains["display"]):
        chain.debug_level = "debug"

    ingestor, ingest_transforms, snap, output, output_transforms = _build(app)

    with caplog.at_level(logging.DEBUG):
        snap.save(apply_transforms(ingest_transforms, ingestor.poll()))
        poll_live(snap, output_transforms, output)

    ran = {record.name for record in caplog.records if "nop:" in record.message}
    assert ran == {"concorde_A.nop", "snapshot_chain.nop", "display.nop"}
