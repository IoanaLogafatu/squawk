"""
tests/test_schema.py

The aircraft schema is the spec's, field for field.

Every module in every chain reads and writes this shape, and snapshot files
on disk are this shape, so drift from the spec is a compatibility break —
this pins the exact serialised form rather than trusting the dataclasses
to stay in step by eye.
"""

from __future__ import annotations

from schemas.aircraft import Aircraft

AIRPORT = ["iata", "icao", "name", "municipality", "country"]

SPEC = {
    "meta":      ["icao_hex", "squawk", "first_seen", "last_seen"],
    "location":  ["latitude", "longitude", "altitude_feet"],
    "direction": ["ground_speed_knots", "heading", "vertical_rate_fpm"],
    "route":     ["callsign", "flight_number", "origin", "destination"],
    "airframe":  ["registration", "type_code", "type_description", "manufacturer", "operator"],
    "airline":   ["airline_name", "airline_iata", "airline_icao", "airline_country"],
    "raw":       [],
}


def test_the_aircraft_has_exactly_the_specs_sections():
    assert list(Aircraft().to_dict()) == list(SPEC)


def test_each_section_has_exactly_the_specs_fields():
    plain = Aircraft().to_dict()

    for section, expected in SPEC.items():
        assert list(plain[section]) == expected, section


def test_airports_have_exactly_the_specs_fields():
    route = Aircraft().to_dict()["route"]

    assert list(route["origin"]) == AIRPORT
    assert list(route["destination"]) == AIRPORT


def test_a_file_written_by_an_older_schema_still_loads():
    """Fields the schema no longer has are ignored, not fatal."""
    old = {
        "meta":  {"icao_hex": "400F6A", "ingest_source": "concorde_A", "reception_type": "adsb_icao"},
        "route": {"callsign": "BAW002", "squawk_code": "2346", "airline": {"name": "BA"}},
        "location": {"latitude": 52.0, "distance_nm": 12.5},
    }

    loaded = Aircraft.from_dict(old)

    assert loaded.meta.icao_hex == "400F6A"
    assert loaded.route.callsign == "BAW002"
    assert loaded.location.latitude == 52.0
