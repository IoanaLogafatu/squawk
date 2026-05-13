"""
display/http_display/server.py

HTTP server internals for the http_display plugin.

SharedState  — thread-safe pub/sub store; the plugin writes, SSE handlers read.
make_handler — returns a configured BaseHTTPRequestHandler subclass.
render_data  — converts Aircraft (or None) to a JSON string for the browser.
"""

from __future__ import annotations

import json
import queue
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from typing import Optional

from schemas.aircraft import Aircraft


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


# ---------------------------------------------------------------------------
# Shared state — written by the plugin thread, read by SSE handler threads
# ---------------------------------------------------------------------------

class SharedState:

    def __init__(self) -> None:
        self._lock        = threading.Lock()
        self._subscribers: list[queue.Queue] = []

    def update(self, a: Optional[Aircraft]) -> None:
        data = render_data(a)
        with self._lock:
            for q in self._subscribers:
                try:
                    q.put_nowait(data)
                except queue.Full:
                    pass

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=4)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------

def make_handler(state: SharedState) -> type:

    class _Handler(BaseHTTPRequestHandler):

        def do_GET(self) -> None:
            if self.path == "/":
                self._serve_page()
            elif self.path == "/events":
                self._serve_events()
            else:
                self.send_error(404)

        def _serve_page(self) -> None:
            body = _PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve_events(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = state.subscribe()
            try:
                while True:
                    try:
                        data = q.get(timeout=15)
                        self.wfile.write(f"data: {data}\n\n".encode())
                    except queue.Empty:
                        self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                state.unsubscribe(q)

        def log_message(self, fmt, *args) -> None:
            pass

    return _Handler


# ---------------------------------------------------------------------------
# Data renderer
# ---------------------------------------------------------------------------

def render_data(a: Optional[Aircraft]) -> str:
    if a is None:
        return "null"

    vr    = a.direction.vertical_rate_fpm or 0
    vrate = "↑" if vr > 200 else "↓" if vr < -200 else "—"

    alt = a.location.altitude_feet
    if alt is None:
        altitude = "—"
    elif alt == 0:
        altitude = "GND"
    else:
        altitude = f"{alt:,} ft"

    dist  = a.location.distance_nm
    card  = _cardinal(a.location.bearing_degrees)
    if dist is None:
        distance = "—"
    elif card:
        distance = f"{dist:.1f} nm {card}"
    else:
        distance = f"{dist:.1f} nm"

    origin = a.route.origin_iata
    dest   = a.route.destination_iata
    if origin and dest:
        route = f"{origin} → {dest}"
    elif origin:
        route = f"{origin} → ?"
    elif dest:
        route = f"? → {dest}"
    else:
        route = None

    return json.dumps({
        "ident":         a.airframe.registration or a.route.callsign or a.meta.icao_hex,
        "aircraft_type": a.airframe.aircraft_type or "—",
        "airline":       a.route.airline_name or None,
        "route":         route,
        "operator":      a.airframe.operator or None,
        "distance":      distance,
        "altitude":      altitude,
        "vrate":         vrate,
        "timestamp":     datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
    })


# ---------------------------------------------------------------------------
# Page HTML
# ---------------------------------------------------------------------------

_PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Squawk</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #111;
    color: #ddd;
    font-family: ui-monospace, "Cascadia Code", "Source Code Pro", monospace;
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 2rem;
  }
  #panel { width: 100%; max-width: 420px; }
  .ident  { font-size: 2.8rem; font-weight: 700; letter-spacing: 0.04em; line-height: 1; margin-bottom: 1.4rem; }
  .vrate  { color: #aaa; font-size: 2rem; font-weight: 400; }
  .row    { margin-bottom: 0.8rem; }
  .label  { font-size: 0.65rem; text-transform: uppercase; letter-spacing: 0.12em; color: #555; margin-bottom: 0.15rem; }
  .value  { font-size: 1.25rem; }
  .ts     { font-size: 0.75rem; color: #444; text-align: right; margin-top: 1.6rem; }
  .empty  { color: #333; font-size: 1.1rem; }
</style>
</head>
<body>
<div id="panel"><p class="empty">Waiting for data&hellip;</p></div>
<script>
const panel = document.getElementById('panel');

function esc(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function row(label, value) {
  return '<div class="row"><div class="label">' + label + '</div>'
       + '<div class="value">' + esc(value) + '</div></div>';
}

function render(d) {
  if (!d) {
    panel.innerHTML = '<p class="empty">No aircraft</p>';
    return;
  }
  panel.innerHTML =
    '<div class="ident">' + esc(d.ident) + ' <span class="vrate">' + esc(d.vrate) + '</span></div>' +
    row('Type',     d.aircraft_type) +
    (d.airline   ? row('Airline',  d.airline)   : '') +
    (d.route     ? row('Route',    d.route)     : '') +
    (d.operator  ? row('Operator', d.operator)  : '') +
    row('Distance', d.distance) +
    row('Altitude', d.altitude) +
    '<div class="ts">' + esc(d.timestamp) + '</div>';
}

const es = new EventSource('/events');
es.onmessage = function(e) {
  try { render(JSON.parse(e.data)); } catch(_) {}
};
</script>
</body>
</html>
"""
