"""
display/http/__init__.py

HTTP display module. Serves a live web page on a configurable port that
auto-updates via Server-Sent Events as new aircraft data arrives.
Supports multiple processor chains feeding separate sections/panels simultaneously.

Configured via [display.http] in config.toml:
    port = 7700
"""

from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer

from display.http.server import SharedState, make_handler
from modules import BaseModule
from schemas.aircraft import Aircraft


_SERVER_LOCK = threading.Lock()
_SERVERS: dict[int, tuple[ThreadingHTTPServer, SharedState]] = {}


class HttpDisplay(BaseModule):

    def __init__(self, cfg: dict) -> None:
        port = int(cfg.get("port", 7700))
        self.panel_id = str(cfg.get("panel_id", "default"))
        self.panel_title = str(cfg.get("panel_title", "Live Traffic"))

        with _SERVER_LOCK:
            if port not in _SERVERS:
                state = SharedState()
                server = ThreadingHTTPServer(("", port), make_handler(state))
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                _SERVERS[port] = (server, state)
                url = f"http://localhost:{port}"
                print(f"  HTTP display active at \033]8;;{url}\033\\{url}\033]8;;\033\\  (ctrl-click to open)")
            else:
                server, state = _SERVERS[port]

        self._state = state

    def process(self, aircraft: list[Aircraft]) -> list[Aircraft]:
        self._state.update(self.panel_id, self.panel_title, aircraft)
        return aircraft


def get(cfg: dict) -> HttpDisplay:
    return HttpDisplay(cfg)

