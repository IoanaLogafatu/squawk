"""
display/epaper_display/renderer.py

Renders an Aircraft (or None) to a 250x122 monochrome PIL image matching
the Waveshare 2.13" V4 e-paper panel layout.
"""

from __future__ import annotations

import time
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from schemas.aircraft import Aircraft

WIDTH, HEIGHT = 250, 122

_FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_FONT_REG  = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def _font(path: str, size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


_CARDINAL_16 = [
    "N", "NNE", "NE", "ENE",
    "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW",
    "W", "WNW", "NW", "NNW",
]

def _cardinal(bearing: Optional[float]) -> Optional[str]:
    if bearing is None:
        return None
    return _CARDINAL_16[int((bearing + 11.25) / 22.5) % 16]


def _arrow(vr: Optional[int]) -> str:
    if vr is None:
        return "—"
    return "↑" if vr > 200 else "↓" if vr < -200 else "—"


def _alt_str(alt: Optional[int]) -> Optional[str]:
    if alt is None:
        return None
    return "GND" if alt == 0 else f"{alt:,} ft"


def _format_country(c: Optional[str]) -> Optional[str]:
    if not c:
        return None
    s = c.strip()
    if s.lower() in ("united kingdom", "uk"):
        return "UK"
    return s


def _route_fmt(iata: Optional[str], country: Optional[str]) -> Optional[str]:
    if not iata:
        return None
    c = _format_country(country)
    return f"{iata.upper()} ({c})" if c else iata.upper()


def _route_str(a: Aircraft) -> Optional[str]:
    orig = _route_fmt(a.route.origin_iata, a.route.origin_country)
    dest = _route_fmt(a.route.destination_iata, a.route.destination_country)
    if orig and dest:
        return f"{orig}  →  {dest}"
    if orig:
        return f"FROM {orig}"
    if dest:
        return f"TO {dest}"
    return None


def render(aircraft: Optional[Aircraft]) -> Image.Image:
    image = Image.new("1", (WIDTH, HEIGHT), 255)
    draw  = ImageDraw.Draw(image)

    big = _font(_FONT_BOLD, 24)
    med = _font(_FONT_BOLD, 16)
    sml = _font(_FONT_REG,  11)

    if aircraft is None:
        draw.text((10, 40), "no aircraft", font=big, fill=0)
        return image

    a = aircraft
    reg      = a.airframe.registration  or a.route.callsign or a.meta.icao_hex
    typ      = a.airframe.aircraft_type or ""
    callsign = (a.route.callsign or "").strip().upper()
    airline  = a.route.airline_name     or a.airframe.operator or ""
    dist     = a.location.distance_nm
    card     = _cardinal(a.location.bearing_degrees)
    alt      = _alt_str(a.location.altitude_feet)
    arrow    = _arrow(a.direction.vertical_rate_fpm)

    # Row 0 (y=2): registration + vertical-rate arrow, right-aligned
    draw.text((8,   2), reg,   font=big, fill=0)
    draw.text((215, 2), arrow, font=big, fill=0)

    # Row 1 (y=32): aircraft type + callsign [EZY18ZQ]
    cs_str = f" [{callsign}]" if callsign else ""
    typ_line = f"{typ}{cs_str}".strip()
    if typ_line:
        if len(typ_line) > 22:
            typ_line = typ_line[:21] + "…"
        draw.text((8, 32), typ_line, font=med, fill=0)

    # Row 2 (y=52): airline name, falling back to registered operator
    if airline:
        airline = airline.upper()
        if len(airline) > 38:
            airline = airline[:37] + "…"
        draw.text((8, 52), airline, font=sml, fill=0)

    # Row 3 (y=66): route origin → destination with countries
    route = _route_str(a)
    if route:
        if len(route) > 42:
            route = route[:41] + "…"
        draw.text((8, 66), route, font=sml, fill=0)


    # Row 4 (y=82): distance + bearing (left), altitude (right)
    if dist is not None:
        dist_str = f"{dist:.1f} nm  {card}" if card else f"{dist:.1f} nm"
        draw.text((8, 82), dist_str, font=med, fill=0)
    if alt:
        alt_w = int(draw.textlength(alt, font=med))
        draw.text((WIDTH - 8 - alt_w, 82), alt, font=med, fill=0)

    # Row 5 (y=108): timestamp, flush right
    ts   = time.strftime("%H:%M:%S")
    ts_w = int(draw.textlength(ts, font=sml))
    draw.text((WIDTH - 8 - ts_w, 108), ts, font=sml, fill=0)

    return image
