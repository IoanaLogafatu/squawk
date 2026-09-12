"""
ingest/concorde/ingestor.py

Ingest module for the Concorde simulator.

Asks the Concorde service where she is and dresses the answer up as an
Aircraft. There is no polling of anything external and no state held here:
the position comes from the service's shared, locked state file, so two
ingest chains configured with this module — concorde_A and concorde_B in
the base build — report the same aircraft in the same place at slightly
different moments, which is precisely the collision the snapshot merge
exists to resolve.

Identity fields are G-BOAC's, and are constant. They are the fields the
merge rule treats as fill-if-blank; the position fields, which the service
recomputes every call, are the ones it always overwrites.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ingest.base import BaseIngest
from schemas.aircraft import (
    Aircraft,
    Airframe,
    Airline,
    Airport,
    Direction,
    Location,
    Meta,
    Route,
    now_utc,
)
from services.concorde.service import ConcordeService

if TYPE_CHECKING:
    from config import Config, IngestChainConfig

# ---------------------------------------------------------------------------
# G-BOAC — Alpha Charlie, the airframe now at Manchester
# ---------------------------------------------------------------------------

ICAO_HEX         = "400F6A"
REGISTRATION     = "G-BOAC"
CALLSIGN         = "BAW002"
FLIGHT_NUMBER    = "BA002"
SQUAWK_CODE      = "2346"
TYPE_CODE        = "CONC"        # ICAO designator for the BAC/Aerospatiale Concorde
TYPE_DESCRIPTION = "Concorde"
CATEGORY         = "A5"          # Heavy, >300,000 lb
MANUFACTURER     = "BAC / Aerospatiale"
OPERATOR         = "British Airways"
RECEPTION_TYPE   = "adsb_icao"


class ConcordeIngest(BaseIngest):

    def __init__(self, name: str, chain: "IngestChainConfig", app: "Config") -> None:
        super().__init__(name, chain, app)
        # One service handle for the life of the process — it holds no
        # connection, only the paths and the observer position.
        self.service = ConcordeService(
            app.data_dir, app.observer_latitude, app.observer_longitude
        )

    def poll(self) -> list[Aircraft]:
        position = self.service.get_position()
        self.log.debug(
            "concorde at %.4f,%.4f  %dft  %.1fnm out  (%.0f%% through the pass)",
            position["latitude"], position["longitude"], position["altitude_feet"],
            position["distance_nm"], position["pass_fraction"] * 100,
        )
        return [self._build(position)]

    def _build(self, position: dict) -> Aircraft:
        return Aircraft(
            meta=Meta(
                icao_hex       = ICAO_HEX,
                ingest_source  = self.chain.name,
                # Stamped here, at observation time, not at write time: this
                # is what the snapshot compares to decide whether an incoming
                # record is newer than the one on disk.
                last_seen      = now_utc(),
                reception_type = RECEPTION_TYPE,
            ),
            location=Location(
                latitude        = position["latitude"],
                longitude       = position["longitude"],
                altitude_feet   = position["altitude_feet"],
                distance_nm     = position["distance_nm"],
                bearing_degrees = position["bearing_degrees"],
                seen_seconds    = 0.0,
            ),
            direction=Direction(
                ground_speed_knots = position["ground_speed_knots"],
                track_degrees      = position["track_degrees"],
                vertical_rate_fpm  = position["vertical_rate_fpm"],
            ),
            route=Route(
                callsign      = CALLSIGN,
                squawk_code   = SQUAWK_CODE,
                flight_number = FLIGHT_NUMBER,
                origin        = Airport(
                    iata="LHR", icao="EGLL", name="London Heathrow Airport",
                    municipality="London", country="United Kingdom"),
                destination   = Airport(
                    iata="JFK", icao="KJFK", name="John F. Kennedy International Airport",
                    municipality="New York", country="United States"),
                airline       = Airline(
                    name=OPERATOR, iata="BA", icao="BAW", country="United Kingdom"),
            ),
            airframe=Airframe(
                registration     = REGISTRATION,
                type_code        = TYPE_CODE,
                type_description = TYPE_DESCRIPTION,
                category         = CATEGORY,
                manufacturer     = MANUFACTURER,
                operator         = OPERATOR,
            ),
            # The service's own answer, kept verbatim — the same courtesy a
            # real ingestor owes a real source's unmapped fields.
            raw={"concorde": position},
        )
