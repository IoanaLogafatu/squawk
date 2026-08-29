import pytest
from modules.vertical_rate_filter import VerticalRateFilter, get
from schemas.aircraft import (
    Aircraft,
    AircraftMeta,
    AircraftLocation,
    AircraftVector,
    AircraftRoute,
    Airframe,
    AircraftRaw,
)


def _make_ac(hex_id: str, vr: float | None) -> Aircraft:
    return Aircraft(
        meta=AircraftMeta(icao_hex=hex_id, ingestor="test"),
        location=AircraftLocation(),
        direction=AircraftVector(vertical_rate_fpm=vr),
        route=AircraftRoute(),
        airframe=Airframe(),
        raw=AircraftRaw(),
    )


def test_climbing_filter():
    f = get({"mode": "climbing"})
    ac_climb = _make_ac("111111", 1500)
    ac_level = _make_ac("222222", 0)
    ac_desc = _make_ac("333333", -1200)

    res = f.process([ac_climb, ac_level, ac_desc])
    assert len(res) == 1
    assert res[0].meta.icao_hex == "111111"


def test_descending_filter():
    f = get({"mode": "descending"})
    ac_climb = _make_ac("111111", 1500)
    ac_level = _make_ac("222222", 0)
    ac_desc = _make_ac("333333", -1200)

    res = f.process([ac_climb, ac_level, ac_desc])
    assert len(res) == 1
    assert res[0].meta.icao_hex == "333333"


def test_custom_min_max_fpm():
    f = get({"min_fpm": 500, "max_fpm": 2000})
    ac1 = _make_ac("111111", 1500)
    ac2 = _make_ac("222222", 2500)
    ac3 = _make_ac("333333", 200)

    res = f.process([ac1, ac2, ac3])
    assert len(res) == 1
    assert res[0].meta.icao_hex == "111111"
