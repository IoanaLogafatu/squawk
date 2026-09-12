"""
tests/test_snapshot_merge.py

The snapshot merge rule.

This is the part of Squawk that has to be right: two ingest processes
write into the same picture with no coordination beyond a lock and this
rule, so every way the rule can be wrong is a way the picture silently
diverges from reality.
"""

from __future__ import annotations

from schemas.aircraft import Airline, Airport, Direction, Location, ensure_utc
from snapshot.file_object import merge


def test_new_aircraft_is_written_as_is(snap, make_aircraft):
    incoming = make_aircraft(age_seconds=0)

    snap.save([incoming])

    held = snap.read_all()
    assert len(held) == 1
    assert held[0].meta.icao_hex == "400F6A"
    assert held[0].route.callsign == "BAW002"


def test_newer_observation_updates_the_record(snap, make_aircraft):
    old = make_aircraft(age_seconds=30, source="concorde_A")
    old.location = Location(latitude=52.0, longitude=-1.0, altitude_feet=2000)
    new = make_aircraft(age_seconds=0, source="concorde_B")
    new.location = Location(latitude=52.5, longitude=-1.5, altitude_feet=9000)

    snap.save([old])
    snap.save([new])

    held = snap.read_all()[0]
    assert held.location.latitude == 52.5
    assert held.location.altitude_feet == 9000
    assert ensure_utc(held.meta.last_seen) == ensure_utc(new.meta.last_seen)


def test_older_observation_is_discarded(snap, make_aircraft):
    new = make_aircraft(age_seconds=0)
    new.location = Location(latitude=52.5, altitude_feet=9000)
    old = make_aircraft(age_seconds=60)
    old.location = Location(latitude=51.0, altitude_feet=1000)

    snap.save([new])
    snap.save([old])   # arrives second, but describes an earlier moment

    held = snap.read_all()[0]
    assert held.location.latitude == 52.5
    assert held.location.altitude_feet == 9000
    assert ensure_utc(held.meta.last_seen) == ensure_utc(new.meta.last_seen)


def test_blank_fields_are_filled_by_a_later_observation(snap, make_aircraft):
    first = make_aircraft(age_seconds=30)
    first.airframe.type_code = None
    first.route.destination = Airport()

    second = make_aircraft(age_seconds=0)
    second.airframe.type_code = "CONC"
    second.route.destination = Airport(iata="JFK", country="United States")

    snap.save([first])
    snap.save([second])

    held = snap.read_all()[0]
    assert held.airframe.type_code == "CONC"
    assert held.route.destination.iata == "JFK"
    assert held.route.destination.country == "United States"


def test_known_fields_are_not_overwritten_by_a_later_observation(snap, make_aircraft):
    enriched = make_aircraft(age_seconds=30)
    enriched.route.airline = Airline(name="British Airways", icao="BAW")

    plain = make_aircraft(age_seconds=0)
    plain.route.airline = Airline(name="Someone Else", icao=None)

    snap.save([enriched])
    snap.save([plain])

    held = snap.read_all()[0]
    # The newer record is newer, not better informed: enrichment survives it.
    assert held.route.airline.name == "British Airways"
    assert held.route.airline.icao == "BAW"


def test_location_direction_and_raw_always_overwrite(make_aircraft):
    existing = make_aircraft(age_seconds=30)
    existing.location  = Location(latitude=52.0, longitude=-1.0, altitude_feet=2000,
                                  distance_nm=40.0)
    existing.direction = Direction(ground_speed_knots=300.0, track_degrees=90.0)
    existing.raw       = {"concorde": {"pass_fraction": 0.1}}

    incoming = make_aircraft(age_seconds=0)
    # A sparser position — but a current one. Blanks must not be back-filled
    # from the old position or the aircraft is placed where it never was.
    incoming.location  = Location(latitude=52.5, longitude=-1.5)
    incoming.direction = Direction(ground_speed_knots=305.0)
    incoming.raw       = {"concorde": {"pass_fraction": 0.6}}

    merged = merge(existing, incoming)

    assert merged.location.latitude == 52.5
    assert merged.location.altitude_feet is None
    assert merged.location.distance_nm is None
    assert merged.direction.track_degrees is None
    assert merged.raw == {"concorde": {"pass_fraction": 0.6}}


def test_merge_does_not_mutate_its_arguments(make_aircraft):
    existing = make_aircraft(age_seconds=30)
    existing.airframe.type_code = None
    incoming = make_aircraft(age_seconds=0)
    incoming.airframe.type_code = "CONC"

    merge(existing, incoming)

    assert existing.airframe.type_code is None


def test_last_seen_always_advances(make_aircraft):
    existing = make_aircraft(age_seconds=30)
    incoming = make_aircraft(age_seconds=0)

    merged = merge(existing, incoming)

    # last_seen is never blank on a stored record, so the fill-if-blank rule
    # would freeze it — and a frozen last_seen expires an aircraft mid-flight.
    assert ensure_utc(merged.meta.last_seen) == ensure_utc(incoming.meta.last_seen)


def test_an_observation_with_no_last_seen_is_never_newer(snap, make_aircraft):
    held = make_aircraft(age_seconds=10)
    held.location = Location(latitude=52.0)
    undateable = make_aircraft(age_seconds=0)
    undateable.meta.last_seen = None
    undateable.location = Location(latitude=99.0)

    snap.save([held])
    snap.save([undateable])

    assert snap.read_all()[0].location.latitude == 52.0


def test_an_aircraft_with_no_hex_is_discarded(snap, make_aircraft):
    nameless = make_aircraft()
    nameless.meta.icao_hex = None

    snap.save([nameless])

    assert snap.read_all() == []


def test_hex_is_normalised_to_one_file(snap, make_aircraft):
    snap.save([make_aircraft(hex_code="400f6a", age_seconds=10)])
    snap.save([make_aircraft(hex_code="400F6A", age_seconds=0)])

    held = snap.read_all()
    assert len(held) == 1
    assert held[0].meta.icao_hex == "400f6a"   # first writer's spelling is kept
    assert (snap.dir / "400F6A.json").exists()


def test_snapshot_survives_a_corrupt_file(snap, make_aircraft):
    snap.save([make_aircraft()])
    (snap.dir / "BADBAD.json").write_text("{ not json")

    held = snap.read_all()

    assert len(held) == 1
    assert held[0].meta.icao_hex == "400F6A"


def test_round_trip_through_disk_preserves_the_record(snap, make_aircraft):
    original = make_aircraft()
    original.route.origin = Airport(iata="LHR", icao="EGLL", municipality="London")
    original.raw = {"concorde": {"pass_fraction": 0.25}}

    snap.save([original])
    held = snap.read_all()[0]

    assert held.to_dict() == original.to_dict()
