"""
tests/test_pipeline.py

Every module type, wired together.

The point of the base build is that all five module types and all three
chain families work as a set, so this walks one observation the whole way
— service, ingest, transform, snapshot, transform, output — and then
walks it back out again through expiry. If a future feature is genuinely
"just add a module", this test keeps passing untouched.

It runs the chains a step at a time rather than starting their loops, so
it tests the wiring without testing the sleep.
"""

from __future__ import annotations

import logging

from ingest import INGESTORS
from output import OUTPUTS
from snapshot import SNAPSHOTS
from transforms import apply_transforms, build_transforms


def _build(app):
    ingest_cfg = app.ingest_chains["concorde_A"]
    output_cfg = app.output_chains["display"]

    return (
        INGESTORS[ingest_cfg.type](ingest_cfg.type, ingest_cfg, app),
        build_transforms(ingest_cfg, app),
        SNAPSHOTS[app.snapshot_chain.type](
            app.snapshot_chain.type, app.snapshot_chain, app),
        OUTPUTS[output_cfg.type](output_cfg.type, output_cfg, app),
        build_transforms(output_cfg, app),
    )


def test_an_observation_travels_from_service_to_console(app, capsys):
    ingestor, ingest_transforms, snap, output, output_transforms = _build(app)

    # -- ingest chain --------------------------------------------------
    observed = ingestor.poll()
    assert len(observed) == 1
    assert observed[0].meta.icao_hex == "400F6A"
    assert observed[0].meta.ingest_source == "concorde_A"
    assert observed[0].location.latitude is not None

    snap.save(apply_transforms(ingest_transforms, observed))

    # -- the snapshot is a folder of files, nothing more ---------------
    assert (app.data_dir / "snapshot" / "400F6A.json").exists()

    # -- output chain --------------------------------------------------
    picture = apply_transforms(output_transforms, snap.read_all())
    output.send(picture)

    printed = capsys.readouterr().out
    assert "400F6A" in printed
    assert "G-BOAC" in printed
    assert "LHR->JFK" in printed


def test_two_ingest_chains_share_one_aircraft(app):
    """
    Two chains, two processes' worth of work, one aircraft in the picture.

    They read the same Concorde service, so they must agree on identity
    and produce one file, not two.
    """
    ingest_a = app.ingest_chains["concorde_A"]
    app.ingest_chains["concorde_B"] = type(ingest_a)(
        name="concorde_B", type=ingest_a.type, transform=["nop"])

    snap = SNAPSHOTS[app.snapshot_chain.type](
        app.snapshot_chain.type, app.snapshot_chain, app)

    for name in ("concorde_A", "concorde_B"):
        chain    = app.ingest_chains[name]
        ingestor = INGESTORS[chain.type](chain.type, chain, app)
        snap.save(ingestor.poll())

    held = snap.read_all()
    assert len(held) == 1
    # The position is the second, newer observation's. The provenance is
    # the first chain's, because ingest_source is a fill-if-blank field
    # under the merge rule — it records who found the aircraft, not who
    # last touched the record.
    assert held[0].meta.ingest_source == "concorde_A"
    assert held[0].location.latitude is not None


def test_an_empty_snapshot_is_a_valid_picture(app, capsys):
    _, _, snap, output, _ = _build(app)

    output.send(snap.read_all())

    assert "0 aircraft" in capsys.readouterr().out


def test_expiry_reaches_the_output_chain(app, capsys):
    ingestor, _, snap, output, _ = _build(app)
    snap.save(ingestor.poll())

    # Expire everything, however fresh.
    snap.chain.expiry_minutes = 0
    snap.expire()

    deleted, _ = snap.read_deletions_since(None)
    output.on_deleted(deleted)

    printed = capsys.readouterr().out
    assert "GONE" in printed
    assert "400F6A" in printed
    assert snap.read_all() == []


def test_transforms_run_in_every_chain_family(app, caplog):
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
        output.send(apply_transforms(output_transforms, snap.read_all()))

    ran = {record.name for record in caplog.records if "nop:" in record.message}
    assert ran == {"concorde_A.nop", "snapshot_chain.nop", "display.nop"}
