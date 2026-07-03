"""
display/http/__init__.py

HTTP display module. Serves a live web page on a configurable port that
auto-updates via Server-Sent Events as new aircraft data arrives.

Configured via [display.http] in config.toml:
    port = 7700
"""

from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer

from display.http.server import SharedState, make_handler
from modules import BaseModule
from schemas.aircraft import Aircraft


class HttpDisplay(BaseModule):

    def __init__(self, cfg: dict) -> None:
        port        = int(cfg.get("port", 7700))
        self._state = SharedState()
        server      = ThreadingHTTPServer(("", port), make_handler(self._state))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://localhost:{port}"
        print(f"  HTTP display  \033]8;;{url}\033\\{url}\033]8;;\033\\  (ctrl-click to open)")

    def process(self, aircraft: list[Aircraft]) -> list[Aircraft]:
        self._state.update(aircraft[0] if aircraft else None)
        return aircraft


def get(cfg: dict) -> HttpDisplay:
    return HttpDisplay(cfg)
