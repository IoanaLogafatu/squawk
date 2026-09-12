"""
ingest/concorde/ingestor.py

Ingest module for the Concorde simulator.

Reads the concorde service's state file and dresses the position up as an
Aircraft. It never calls the service: it reads whatever the service last
published, so two ingest chains configured with this module — concorde_A
and concorde_B in the base build — report the same aircraft from the same
flight at different moments, which is the collision the snapshot merge
exists to resolve.

last_seen is the time the service computed the position, not the time this
module read it. That is when the observation was actually made, and it is
what makes a stopped service look like an aircraft that has stopped being
seen: no newer position arrives, so the snapshot's expiry takes her away.
For the same reason a poll that finds nothing newer than last time reports
nothing at all.

Identity fields are G-BOAC's, and are constant. The service's own answer is
kept verbatim in raw, under this chain's name — including what the schema
has no field for, such as her range and bearing from the observer.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

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
    ensure_utc,
)
from services.base import read_state

if TYPE_CHECKING:
    from config import Config, IngestChainConfig

# ---------------------------------------------------------------------------
# G-BOAC — Alpha Charlie, the airframe now at Manchester
# ---------------------------------------------------------------------------

ICAO_HEX         = "400F6A"
REGISTRATION     = "G-BOAC"
CALLSIGN         = "BAW002"
FLIGHT_NUMBER    = "BA002"
SQUAWK           = "2346"
TYPE_CODE        = "CONC"        # ICAO designator for the BAC/Aerospatiale Concorde
TYPE_DESCRIPTION = "Concorde"
MANUFACTURER     = "BAC / Aerospatiale"
OPERATOR         = "British Airways"

SERVICE = "concorde"

# Position fields this module reads — a state file missing any of them is
# treated as no state at all.
REQUIRED_POSITION = (
    "latitude", "longitude", "altitude_feet", "ground_speed_knots", "heading",
    "vertical_rate_fpm", "distance_nm", "pass_fraction",
)


class ConcordeIngest(BaseIngest):

    def __init__(self, name: str, chain: "IngestChainConfig", app: "Config") -> None:
        super().__init__(name, chain, app)
        # When the last position this chain reported was computed. A poll
        # that finds the same one again has nothing new to say.
        self._last_observed: datetime | None = None
        # Whether the "no data from the service" warning is currently up,
        # so a stopped service is reported once rather than every poll.
        self._warned = False

    def poll(self) -> list[Aircraft]:
        state = read_state(self.app.data_dir, SERVICE)
        reading = _parse(state)

        if reading is None:
            self._warn("no usable state from the %s service — is it running? "
                       "(python main.py service %s)", SERVICE, SERVICE)
            return []

        observed_at, position = reading
        self.log.debug("read %s service state computed at %s", SERVICE, observed_at.isoformat())

        if observed_at == self._last_observed:
            self._warn("the %s service has published nothing since the last poll — "
                       "has it stopped?", SERVICE)
            return []

        if self._warned:
            self.log.info("the %s service is publishing again", SERVICE)
            self._warned = False
        self._last_observed = observed_at

        self.log.debug(
            "concorde at %.4f,%.4f  %dft  %.1fnm out  (%.0f%% through the pass)",
            position["latitude"], position["longitude"], position["altitude_feet"],
            position["distance_nm"], position["pass_fraction"] * 100,
        )
        return [self._build(observed_at, position)]

    def _warn(self, message: str, *args: Any) -> None:
        if not self._warned:
            self.log.warning(message, *args)
            self._warned = True

    def _build(self, observed_at: datetime, position: dict[str, Any]) -> Aircraft:
        return Aircraft(
            meta=Meta(
                icao_hex  = ICAO_HEX,
                squawk    = SQUAWK,
                # The service's observation time, not the read time — see
                # the module docstring. first_seen is left to the snapshot,
                # which knows when the record began.
                last_seen = observed_at,
            ),
            location=Location(
                latitude      = position["latitude"],
                longitude     = position["longitude"],
                altitude_feet = position["altitude_feet"],
            ),
            direction=Direction(
                ground_speed_knots = position["ground_speed_knots"],
                heading            = position["heading"],
                vertical_rate_fpm  = position["vertical_rate_fpm"],
            ),
            route=Route(
                callsign      = CALLSIGN,
                flight_number = FLIGHT_NUMBER,
                origin        = Airport(
                    iata="LHR", icao="EGLL", name="London Heathrow Airport",
                    municipality="London", country="United Kingdom"),
                destination   = Airport(
                    iata="JFK", icao="KJFK", name="John F. Kennedy International Airport",
                    municipality="New York", country="United States"),
            ),
            airframe=Airframe(
                registration     = REGISTRATION,
                type_code        = TYPE_CODE,
                type_description = TYPE_DESCRIPTION,
                manufacturer     = MANUFACTURER,
                operator         = OPERATOR,
            ),
            airline=Airline(
                airline_name    = OPERATOR,
                airline_iata    = "BA",
                airline_icao    = "BAW",
                airline_country = "United Kingdom",
            ),
            # The service's own answer, kept verbatim under this chain's name
            # — the same courtesy a real ingestor owes a real source's
            # unmapped fields. Keyed per chain so the snapshot keeps
            # concorde_A's and concorde_B's side by side.
            raw={self.chain.name: position},
        )


def _parse(state: dict[str, Any] | None) -> tuple[datetime, dict[str, Any]] | None:
    """The observation time and position out of a state file, or None if unusable."""
    if state is None:
        return None
    try:
        observed_at = ensure_utc(datetime.fromisoformat(state["observed_at"]))
        position    = state["position"]
    except (KeyError, TypeError, ValueError):
        return None
    if not isinstance(position, dict) or not all(key in position for key in REQUIRED_POSITION):
        return None
    return observed_at, position
