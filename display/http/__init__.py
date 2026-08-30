"""
display/http/__init__.py

HTTP display module. Serves a live web page on a configurable port that
auto-updates via Server-Sent Events as new aircraft data arrives.
Supports multiple processor chains feeding separate sections/panels simultaneously.

Configured via [display.http] in config.toml:
    port = 7700

    [display.http.panels.<chain_name>]
    title = "..."
    order = 1

Each chain is matched to a panel block by chain name. Missing panel blocks
fall back to a title-cased chain name and order 999.
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
        self.chain_name = str(cfg.get("chain_name", "default"))

        panels = cfg.get("panels", {}) or {}
        panel_cfg = panels.get(self.chain_name)
        if panel_cfg is None:
            print(f"  http display: no panel config for chain {self.chain_name!r} — using defaults")
            panel_cfg = {}
        self.panel_title = str(panel_cfg.get("title", self.chain_name.replace("_", " ").title()))
        self.panel_order = int(panel_cfg.get("order", 999))

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
        self._state.update(self.chain_name, self.panel_title, self.panel_order, aircraft)
        return aircraft


def get(cfg: dict) -> HttpDisplay:
    return HttpDisplay(cfg)
