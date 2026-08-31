# readsb / tar1090 `aircraft.json` — Field Reference

Source URL (typical): `http://<receiver>/tar1090/data/aircraft.json`

The file contains a single snapshot of all currently-tracked aircraft. Top-level fields describe the snapshot itself; each entry in the `aircraft` array describes one aircraft.

## Top-level fields

| Field | Description | Sample |
|---|---|---|
| `now` | Snapshot time, Unix epoch seconds (float, ms precision) | `1777483919.000` |
| `messages` | Total messages received by the receiver since startup | `50451390` |
| `aircraft` | Array of aircraft records (see below) | `[ {...}, {...} ]` |

## Aircraft fields

**Required** = appears on every aircraft record.
**Optional** = often missing — depends on aircraft type, transponder version, and what's been heard recently.

| Field | Description | Required? | Sample |
|---|---|---|---|
| `hex` | ICAO 24-bit address (6 hex digits) | Required | `"4cade0"` |
| `type` | How this data was obtained: `adsb_icao`, `adsb_icao_nt`, `adsr_icao`, `tisb_icao`, `mlat`, `mode_s`, `adsb_other`, `adsr_other`, `tisb_other`, `tisb_trackfile` | Required | `"adsb_icao"` |
| `flight` | Callsign (8 chars, space-padded — strip trailing spaces) | Optional | `"RYR54NN "` |
| `r` | Registration (tail number) — *DB enrichment only* | Optional | `"EI-IHI"` |
| `t` | Aircraft type code (ICAO) — *DB enrichment only* | Optional | `"B738"` |
| `desc` | Aircraft type description — *DB enrichment only* | Optional | `"BOEING 737-800"` |
| `dbFlags` | Bitfield: 1=military, 2=interesting, 4=PIA, 8=LADD — *DB enrichment only* | Optional | `0` |
| `alt_baro` | Barometric altitude (feet). Can also be the string `"ground"` for aircraft on the ground | Optional | `24825` |
| `alt_geom` | Geometric (GNSS/WGS84) altitude (feet) | Optional | `25650` |
| `baro_rate` | Barometric vertical rate (ft/min, +ve = climb) | Optional | `-4608` |
| `geom_rate` | Geometric vertical rate (ft/min) | Optional | `-4800` |
| `gs` | Ground speed (knots) | Optional | `457.4` |
| `ias` | Indicated airspeed (knots) | Optional | `298` |
| `tas` | True airspeed (knots) | Optional | `428` |
| `mach` | Mach number | Optional | `0.708` |
| `track` | Ground track / direction of motion (° true) | Optional | `338.31` |
| `track_rate` | Rate of change of track (°/sec) | Optional | `-0.03` |
| `mag_heading` | Magnetic heading where the aircraft is pointing (°) | Optional | `340.66` |
| `true_heading` | True heading where the aircraft is pointing (°) | Optional | `341.38` |
| `roll` | Roll angle (°, +ve = right wing down) | Optional | `-0.18` |
| `wd` | Wind direction — *meteorological convention, "from"* (° true) | Optional | `105` |
| `ws` | Wind speed (knots) | Optional | `53` |
| `oat` | Outside (static) air temperature (°C) | Optional | `-32` |
| `tat` | Total air temperature (°C, includes ram heating) | Optional | `-8` |
| `lat` | Latitude (° decimal) | Optional | `52.714233` |
| `lon` | Longitude (° decimal) | Optional | `-1.426620` |
| `squawk` | Mode A transponder code (4-digit octal, as a string) | Optional | `"5310"` |
| `emergency` | Emergency state: `none`, `general`, `lifeguard`, `minfuel`, `nordo`, `unlawful`, `downed`, `reserved` | Optional | `"none"` |
| `category` | ICAO ADS-B emitter category — see the table below | Optional | `"A3"` |
| `nav_qnh` | Altimeter pressure setting reported by aircraft (hPa) | Optional | `1013.6` |
| `nav_altitude_mcp` | Selected altitude on the autopilot panel (MCP/FCU) (feet) | Optional | `20000` |
| `nav_altitude_fms` | Selected altitude in the Flight Management System (feet) | Optional | `20000` |
| `nav_heading` | Selected heading on the autopilot (°) | Optional | `341.72` |
| `nav_modes` | Active autopilot modes: array containing any of `autopilot`, `vnav`, `althold`, `approach`, `lnav`, `tcas` | Optional | `["autopilot","tcas"]` |
| `version` | ADS-B protocol version: `0`, `1`, or `2` | Optional | `2` |
| `nic` | Navigation Integrity Category (0–11, higher = better; defines containment radius) | Optional | `8` |
| `nic_baro` | Barometric altitude integrity flag — 1 = cross-checked, 0 = not | Optional | `1` |
| `nac_p` | Navigation Accuracy — Position (0–11; e.g. 11 ≈ EPU < 3 m) | Optional | `11` |
| `nac_v` | Navigation Accuracy — Velocity (0–4; e.g. 2 ≈ < 3 m/s) | Optional | `2` |
| `sil` | Source Integrity Level (0–3) — probability that NIC has been exceeded | Optional | `3` |
| `sil_type` | Basis for SIL: `unknown`, `persample`, `perhour` | Optional | `"perhour"` |
| `gva` | Geometric Vertical Accuracy (0–2) | Optional | `2` |
| `sda` | System Design Assurance (0–3) — failure rate of avionics | Optional | `2` |
| `rc` | Radius of containment for position (metres) | Optional | `186` |
| `alert` | Alert flag (transponder squawk-ident change) — 0 or 1 | Optional | `0` |
| `spi` | Special Position Identifier flag (pilot pressed IDENT) — 0 or 1 | Optional | `0` |
| `mlat` | List of fields whose values came from MLAT (multilateration) rather than ADS-B | Required (may be `[]`) | `["altitude","gs","lat","lon"]` |
| `tisb` | List of fields whose values came from TIS-B (ground-relayed traffic info) | Required (may be `[]`) | `[]` |
| `messages` | Total messages received from this aircraft | Required | `4529` |
| `seen` | Seconds since any message from this aircraft | Required | `9.8` |
| `seen_pos` | Seconds since last position fix from this aircraft | Optional | `24.127` |
| `rssi` | Recent average signal power (dBFS, always negative) | Required | `-23.7` |
| `r_dst` | *(readsb extension)* Distance from receiver (nautical miles) | Optional | `25.533` |
| `r_dir` | *(readsb extension)* Bearing from receiver (° true) | Optional | `276.4` |

## Emitter category (`category`)

Mapped to `airframe.category`. It answers "what kind of aircraft is this" directly, which altitude only approximates — a bizjet at 30,000 ft is not an airliner, and only the category says so.

| Code | Meaning |
|---|---|
| `A0` | No information |
| `A1` | Light — under 15,500 lb |
| `A2` | Small — 15,500 to 75,000 lb |
| `A3` | Large — 75,000 to 300,000 lb |
| `A4` | High-vortex large (B757) |
| `A5` | Heavy — over 300,000 lb |
| `A6` | High performance — over 5g and over 400 kt |
| `A7` | Rotorcraft |
| `B1` | Glider / sailplane |
| `B2` | Lighter-than-air |
| `B3` | Parachutist / skydiver |
| `B4` | Ultralight / hang-glider / paraglider |
| `B6` | UAV |
| `B7` | Space / trans-atmospheric vehicle |
| `C0`–`C7` | Surface vehicles and fixed obstacles (emergency and service vehicles, tethered balloons, point obstacles) |

**Treat absence as normal, not as an error.** The value is configured by the operator on the transponder, so it is occasionally wrong; `A0` ("no information") is common; and Mode S-only and MLAT tracks do not carry it at all. In a live snapshot roughly three-quarters of aircraft reported a usable value. Anything consuming it needs a sensible answer for "unknown".

## Notes

- **Truly required fields are minimal.** Only `hex`, `type`, `messages`, `seen`, `mlat`, and `tisb` are guaranteed across every aircraft type. A pure Mode-S aircraft may have nothing else.
- **MLAT records** carry the `mlat` array listing which fields were multilaterated. Position is included but `nic: 0, rc: 0` flags it as low-integrity.
- **DB-enrichment fields** (`r`, `t`, `desc`, `dbFlags`) only appear if the receiver is configured with a static aircraft database. They are not in the raw ADS-B feed.
- **`alt_baro` is polymorphic** — usually an integer (feet), but the string `"ground"` when the aircraft is on the ground.
- **Pass-through rule:** ingest every field that exists; never invent missing ones. Missing means "we don't know," which is genuinely different from zero.
