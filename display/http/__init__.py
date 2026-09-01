"""
display/http/__init__.py

HTTP display module. Serves a live web page on a configurable port that
auto-updates via Server-Sent Events as new aircraft data arrives.
Supports multiple processor chains feeding separate sections/panels simultaneously.

Configured via [display.http] in config.toml:
    port = 7700

    [display.http.panels.<chain_name>]
    title  = "..."
    slot   = 1
    layout = "list"              # "card" (default) or "list"
    bands  = ["D", "C", "B", "A"]

Each chain is matched to a panel block by chain name. `slot` is a fixed
position in the 4x2 wall (1-4 across the top row, 5-8 across the bottom);
the config loader requires it and rejects duplicates. `title` is optional
and falls back to a title-cased chain name.

`layout` selects the panel's renderer and defaults to "card", the
single-aircraft view every existing panel uses. "list" renders one row per
band letter in `bands`, top to bottom as written, and requires the
altitude_band enricher upstream — rows are placed by each aircraft's own
location.altitude_band, never by its position in the list.
"""

from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer

from display.http.server import SharedState, make_handler
from modules import BaseModule, ModuleContext
from schemas.aircraft import Aircraft


_SERVER_LOCK = threading.Lock()
_SERVERS: dict[int, tuple[ThreadingHTTPServer, SharedState]] = {}


class HttpDisplay(BaseModule):

    def __init__(self, cfg: dict) -> None:
        port = int(cfg.get("port", 7700))
        self.chain_name = str(cfg.get("chain_name", "default"))

        panels = cfg.get("panels", {}) or {}
        panel_cfg = panels.get(self.chain_name, {})
        self.panel_title = str(panel_cfg.get("title", self.chain_name.replace("_", " ").title()))
        self.slot = int(panel_cfg.get("slot", 0))
        # Validated by the config loader; defaulted here so a panel block that
        # predates the list layout keeps rendering as a card.
        self.layout = str(panel_cfg.get("layout", "card"))
        self.bands = [str(b) for b in (panel_cfg.get("bands") or [])]

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
        self._state.update(self.chain_name, self.panel_title, self.slot, aircraft,
                           layout=self.layout, bands=self.bands)
        return aircraft


def get(cfg: dict, ctx: ModuleContext) -> HttpDisplay:
    return HttpDisplay(cfg)
