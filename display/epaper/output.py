"""
display/epaper_display/output.py

Combined output for the epaper_display module:
  - saves each rendered frame as a PNG to data/display/epaper_display/
  - serves the PNG via a simple HTTP page (auto-refreshing)
  - pushes to Waveshare 2.13" V4 hardware when the driver is available
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from PIL import Image


class EpaperOutput:

    def __init__(self, png_path: Path, full_refresh_every: int = 600) -> None:
        self._png_path = png_path
        self._png_path.parent.mkdir(parents=True, exist_ok=True)
        self._full_refresh_every = full_refresh_every
        self._cycles = 0
        self._epd = None
        try:
            from waveshare_epd import epd2in13_V4
            self._epd = epd2in13_V4.EPD()
            self._epd.init()
            self._epd.Clear(0xFF)
            self._epd.init_fast()
        except ImportError:
            pass

    def write(self, image: Image.Image) -> None:
        image.save(self._png_path)
        if self._epd:
            self._cycles += 1
            if self._cycles >= self._full_refresh_every:
                self._epd.init()
                self._epd.Clear(0xFF)
                self._epd.init_fast()
                self._cycles = 0
            self._epd.displayPartial(self._epd.getbuffer(image))


def start_http_server(png_path: Path, port: int) -> None:
    """Serve the rendered PNG on a background daemon thread."""

    def make_handler() -> type:
        class _Handler(BaseHTTPRequestHandler):

            def do_GET(self) -> None:
                if self.path == "/display.png":
                    self._serve_png()
                elif self.path == "/":
                    self._serve_page()
                else:
                    self.send_error(404)

            def _serve_png(self) -> None:
                try:
                    data = png_path.read_bytes()
                except FileNotFoundError:
                    self.send_error(503, "No image yet")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def _serve_page(self) -> None:
                body = _PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args) -> None:
                pass

        return _Handler

    server = HTTPServer(("", port), make_handler())
    threading.Thread(target=server.serve_forever, daemon=True).start()


_PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="10">
<title>Squawk — e-paper</title>
<style>
  body { margin: 0; background: #111; display: flex; align-items: center;
         justify-content: center; min-height: 100vh; }
  img  { image-rendering: pixelated; width: 500px; height: 244px;
         border: 1px solid #333; }
</style>
</head>
<body>
<img src="/display.png" alt="e-paper display">
</body>
</html>
"""
