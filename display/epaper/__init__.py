"""
display/epaper/__init__.py

Display plugin that renders the nearest aircraft onto a 250x122 monochrome
image, saves it as a PNG, serves it via HTTP, and (when hardware is present)
pushes it to a Waveshare 2.13" V4 e-paper panel.

Configured via [display.epaper] in config.toml:
    port               = 7700   # HTTP port for the PNG preview page
    full_refresh_every = 600    # cycles between full e-paper refreshes
    invert             = false  # true rotates 180° (power lead at top)

PNG is always written to {data_dir}/display/epaper/squawk_display.png.

Only re-renders when the displayed data changes, to avoid unnecessary
e-paper flicker and wear.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from plugins import BasePlugin
from display.epaper.output import EpaperOutput, start_http_server
from display.epaper.renderer import render
from schemas.aircraft import Aircraft


def _signature(a: Optional[Aircraft]) -> tuple:
    """Summarise what would be shown. Unchanged signature = no redraw needed."""
    if a is None:
        return ("none",)
    vr = a.direction.vertical_rate_fpm or 0
    return (
        a.airframe.registration,
        a.airframe.aircraft_type,
        a.airframe.operator,
        round(a.location.distance_nm or 0, 1),
        a.location.altitude_feet,
        1 if vr > 200 else -1 if vr < -200 else 0,
    )


class EpaperDisplay(BasePlugin):

    def __init__(self, cfg: dict) -> None:
        from config import config as squawk_config
        data_dir = Path(cfg.get("data_dir", squawk_config.squawk.data_dir))
        png_path = data_dir / "display" / "epaper" / "squawk_display.png"
        port     = int(cfg.get("port", 7700))

        self._output         = EpaperOutput(png_path, cfg.get("full_refresh_every", 600))
        self._invert         = bool(cfg.get("invert", False))
        self._last_signature = None

        if port:
            start_http_server(png_path, port)
            url = f"http://localhost:{port}"
            print(f"  E-paper preview  \033]8;;{url}\033\\{url}\033]8;;\033\\  (ctrl-click to open)")

    def process(self, aircraft: list[Aircraft]) -> list[Aircraft]:
        a   = aircraft[0] if aircraft else None
        sig = _signature(a)
        if sig != self._last_signature:
            image = render(a)
            if self._invert:
                image = image.rotate(180)
            self._output.write(image)
            self._last_signature = sig
        return aircraft


def get(cfg: dict) -> EpaperDisplay:
    return EpaperDisplay(cfg)
