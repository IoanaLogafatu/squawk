"""
tests/test_snapshot_merge.py

The snapshot merge rule.

This is the part of Squawk that has to be right: two ingest processes
write into the same picture with no coordination beyond a lock and this
rule, so every way the rule can be wrong is a way the picture silently
diverges from reality.

The rule is three independent rules, and the tests are grouped by them:

  1. raw[<module>] — replaced per key, unconditionally.
  2. location, direction, last_seen — only from a newer observation.
  3. everything else — known incoming values overwrite, blanks are skipped,
     no date check.
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


# ---------------------------------------------------------------------------
# Rule 2 — the position set is gated on last_seen
# ---------------------------------------------------------------------------

def test_an_older_observation_does_not_move_the_aircraft(snap, make_aircraft):
    new = make_aircraft(age_seconds=0)
    new.location  = Location(latitude=52.5, altitude_feet=9000)
    new.direction = Direction(heading=90.0)
    old = make_aircraft(age_seconds=60)
    old.location  = Location(latitude=51.0, altitude_feet=1000)
    old.direction = Direction(heading=270.0)

    snap.save([new])
    snap.save([old])   # arrives second, but describes an earlier moment

    held = snap.read_all()[0]
    assert held.location.latitude == 52.5
    assert held.location.altitude_feet == 9000
    assert held.direction.heading == 90.0
    assert ensure_utc(held.meta.last_seen) == ensure_utc(new.meta.last_seen)


def test_an_equally_old_observation_does_not_move_the_aircraft(make_aircraft):
    """Newer means strictly newer — a tie keeps what is held."""
    existing = make_aircraft()
    existing.location = Location(latitude=52.5)
    incoming = make_aircraft()
    incoming.meta.last_seen = existing.meta.last_seen
    incoming.location = Location(latitude=51.0)

    assert merge(existing, incoming).location.latitude == 52.5


# ---------------------------------------------------------------------------
# Rule 3 — everything else: known values overwrite, blanks are skipped
# ---------------------------------------------------------------------------

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


def test_known_fields_are_overwritten_by_a_later_report(snap, make_aircraft):
    """Not frozen after the first write — a later report with data replaces it."""
    first = make_aircraft(age_seconds=30)
    first.airline = Airline(airline_name="Speedbird", airline_icao="BAW")

    corrected = make_aircraft(age_seconds=0)
    corrected.airline = Airline(airline_name="British Airways", airline_icao=None)

    snap.save([first])
    snap.save([corrected])

    held = snap.read_all()[0]
    assert held.airline.airline_name == "British Airways"
    # The blank in the later report is skipped, not written over the value.
    assert held.airline.airline_icao == "BAW"


def test_known_fields_overwrite_even_from_an_older_observation(snap, make_aircraft):
    """Rule 3 has no date check — only the position set does."""
    current = make_aircraft(age_seconds=0)
    current.location = Location(latitude=52.5)
    current.airframe.type_code = None

    late_enrichment = make_aircraft(age_seconds=120)
    late_enrichment.location = Location(latitude=10.0)
    late_enrichment.airframe.type_code = "CONC"
    late_enrichment.route.callsign = "BAW1"

    snap.save([current])
    snap.save([late_enrichment])

    held = snap.read_all()[0]
    assert held.airframe.type_code == "CONC"
    assert held.route.callsign == "BAW1"
    assert held.location.latitude == 52.5


def test_blank_incoming_fields_never_erase_known_ones(make_aircraft):
    existing = make_aircraft(age_seconds=30)
    existing.meta.squawk = "2346"
    existing.airline = Airline(airline_name="British Airways")
    existing.route.origin = Airport(iata="LHR", icao="EGLL")

    incoming = make_aircraft(age_seconds=0)
    incoming.route = type(incoming.route)()
    incoming.airframe = type(incoming.airframe)()
    incoming.meta.squawk = None

    merged = merge(existing, incoming)

    assert merged.meta.squawk == "2346"
    assert merged.airline.airline_name == "British Airways"
    assert merged.route.callsign == "BAW002"
    assert merged.route.origin.iata == "LHR"
    assert merged.airframe.registration == "G-BOAC"


def test_a_newer_position_replaces_the_whole_set(make_aircraft):
    existing = make_aircraft(age_seconds=30)
    existing.location  = Location(latitude=52.0, longitude=-1.0, altitude_feet=2000)
    existing.direction = Direction(ground_speed_knots=300.0, heading=90.0)

    incoming = make_aircraft(age_seconds=0)
    # A sparser position — but a current one. Blanks must not be back-filled
    # from the old position or the aircraft is placed where it never was.
    incoming.location  = Location(latitude=52.5, longitude=-1.5)
    incoming.direction = Direction(ground_speed_knots=305.0)

    merged = merge(existing, incoming)

    assert merged.location.latitude == 52.5
    assert merged.location.altitude_feet is None
    assert merged.direction.heading is None


# ---------------------------------------------------------------------------
# Rule 1 — raw merges per module key, unconditionally
# ---------------------------------------------------------------------------

def test_raw_keeps_every_sources_payload(snap, make_aircraft):
    snap.save([make_aircraft(age_seconds=10, source="concorde_A")])
    snap.save([make_aircraft(age_seconds=0,  source="concorde_B")])

    held = snap.read_all()[0]
    assert sorted(held.raw) == ["concorde_A", "concorde_B"]


def test_a_sources_own_raw_key_is_replaced(make_aircraft):
    existing = make_aircraft(age_seconds=30)
    existing.raw = {"concorde_A": {"pass_fraction": 0.1}, "adsbwest": {"rssi": -3}}

    incoming = make_aircraft(age_seconds=0)
    incoming.raw = {"concorde_A": {"pass_fraction": 0.6}}

    merged = merge(existing, incoming)

    assert merged.raw == {"concorde_A": {"pass_fraction": 0.6}, "adsbwest": {"rssi": -3}}


def test_raw_is_written_even_from_an_older_observation(snap, make_aircraft):
    snap.save([make_aircraft(age_seconds=0,  source="concorde_A")])
    snap.save([make_aircraft(age_seconds=60, source="concorde_B")])

    held = snap.read_all()[0]
    assert held.raw["concorde_B"] == {"age_seconds": 60}


# ---------------------------------------------------------------------------
# first_seen — the earliest on offer
# ---------------------------------------------------------------------------

def test_a_new_record_is_stamped_first_seen_from_last_seen(snap, make_aircraft):
    incoming = make_aircraft(age_seconds=10)

    snap.save([incoming])

    held = snap.read_all()[0]
    assert ensure_utc(held.meta.first_seen) == ensure_utc(incoming.meta.last_seen)
    assert incoming.meta.first_seen is None, "save() must not modify what it was given"


def test_first_seen_stays_put_as_newer_observations_arrive(snap, make_aircraft):
    first = make_aircraft(age_seconds=30)
    snap.save([first])
    snap.save([make_aircraft(age_seconds=0)])

    held = snap.read_all()[0]
    assert ensure_utc(held.meta.first_seen) == ensure_utc(first.meta.last_seen)


def test_a_delayed_older_report_moves_first_seen_earlier(snap, make_aircraft):
    snap.save([make_aircraft(age_seconds=10)])
    delayed = make_aircraft(age_seconds=90, source="concorde_B")
    snap.save([delayed])

    held = snap.read_all()[0]
    assert ensure_utc(held.meta.first_seen) == ensure_utc(delayed.meta.last_seen)


def test_a_sources_own_first_seen_is_used_if_earlier(make_aircraft):
    from datetime import timedelta

    existing = make_aircraft(age_seconds=30)
    existing.meta.first_seen = existing.meta.last_seen
    incoming = make_aircraft(age_seconds=0)
    incoming.meta.first_seen = existing.meta.last_seen - timedelta(minutes=10)

    merged = merge(existing, incoming)

    assert merged.meta.first_seen == incoming.meta.first_seen


# ---------------------------------------------------------------------------
# Generally
# ---------------------------------------------------------------------------


def test_merge_does_not_mutate_its_arguments(make_aircraft):
    existing = make_aircraft(age_seconds=30)
    existing.airframe.type_code = None
    incoming = make_aircraft(age_seconds=0)
    incoming.airframe.type_code = "CONC"

    merge(existing, incoming)

    assert existing.airframe.type_code is None


def test_last_seen_advances_with_a_newer_observation(make_aircraft):
    existing = make_aircraft(age_seconds=30)
    incoming = make_aircraft(age_seconds=0)

    merged = merge(existing, incoming)

    # A frozen last_seen would expire an aircraft mid-flight.
    assert ensure_utc(merged.meta.last_seen) == ensure_utc(incoming.meta.last_seen)


def test_last_seen_never_goes_backwards(make_aircraft):
    existing = make_aircraft(age_seconds=0)
    incoming = make_aircraft(age_seconds=30)

    merged = merge(existing, incoming)

    assert ensure_utc(merged.meta.last_seen) == ensure_utc(existing.meta.last_seen)


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
    # icao_hex is a rule-3 field: the latest writer's spelling is held,
    # while the filename stays the normalised one.
    assert held[0].meta.icao_hex == "400F6A"
    assert (snap.dir / "400F6A.json").exists()


def test_snapshot_survives_a_corrupt_file(snap, make_aircraft):
    snap.save([make_aircraft()])
    (snap.dir / "BADBAD.json").write_text("{ not json")

    held = snap.read_all()

    assert len(held) == 1
    assert held[0].meta.icao_hex == "400F6A"


def test_an_unchanged_record_is_not_rewritten(snap, make_aircraft):
    incoming = make_aircraft()
    snap.save([incoming])
    path = snap.dir / "400F6A.json"
    before = path.stat().st_ino   # every write is a rename to a fresh file

    snap.save([incoming])   # the same observation again

    assert path.stat().st_ino == before


def test_round_trip_through_disk_preserves_the_record(snap, make_aircraft):
    original = make_aircraft()
    original.meta.first_seen = original.meta.last_seen
    original.airline = Airline(airline_name="British Airways", airline_icao="BAW")
    original.route.origin = Airport(iata="LHR", icao="EGLL", municipality="London")
    original.raw = {"concorde_A": {"pass_fraction": 0.25}}

    snap.save([original])
    held = snap.read_all()[0]

    assert held.to_dict() == original.to_dict()
