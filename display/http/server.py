"""
display/http/server.py

HTTP server internals for the http display module.
Supports multi-channel / multi-panel grid layouts for large TV displays
and responsive web viewing via Server-Sent Events (SSE).
"""

from __future__ import annotations

import json
import queue
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from typing import Optional

import system
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
# Data renderers
# ---------------------------------------------------------------------------

def render_aircraft_dict(a: Aircraft) -> dict:
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

    orig_country = a.route.origin_country
    dest_country = a.route.destination_country
    if orig_country and orig_country.lower() in ("united kingdom", "uk"):
        orig_country = "UK"
    if dest_country and dest_country.lower() in ("united kingdom", "uk"):
        dest_country = "UK"

    return {
        "ident":               a.airframe.registration or a.route.callsign or a.meta.icao_hex,
        "registration":        a.airframe.registration,
        "callsign":            a.route.callsign,
        "icao_hex":            a.meta.icao_hex,
        "type_code":           a.airframe.type_code,
        "type_description":    a.airframe.type_description,
        "category":            a.airframe.category,
        "manufacturer":        a.airframe.manufacturer or None,
        "airline":             a.route.airline_name or None,
        "route":               route,
        "origin_iata":         origin,
        "origin_country":      orig_country,
        "destination_iata":    dest,
        "destination_country": dest_country,
        "operator":            a.airframe.operator or None,
        "distance":            distance,
        "distance_nm":         dist,
        "altitude":            altitude,
        "altitude_feet":       alt,
        "vrate":               vrate,
        "speed_knots":         round(a.direction.ground_speed_knots) if a.direction.ground_speed_knots else None,
        "timestamp":           datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
    }


# ---------------------------------------------------------------------------
# Shared state — multi-panel hub
# ---------------------------------------------------------------------------

class SharedState:

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue] = []
        self._panels: dict[str, dict] = {}

    def update(self, chain_name: str, panel_title: str, slot: int,
               aircraft: list[Aircraft]) -> None:
        with self._lock:
            self._panels[chain_name] = {
                "chain_name":    chain_name,
                "title":         panel_title,
                "slot":          slot,
                "aircraft":      [render_aircraft_dict(a) for a in aircraft],
                "count":         len(aircraft),
                "updated_epoch": time.time(),
            }
            payload = self._get_payload()

        with self._lock:
            for q in self._subscribers:
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    pass

    def _get_payload(self) -> str:
        return json.dumps({
            "panels":    self._panels,
            "system":    system.snapshot(),
            "timestamp": datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
        })

    def get_current_payload(self) -> str:
        with self._lock:
            return self._get_payload()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=16)
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
            if self.path == "/" or self.path == "/index.html":
                self._serve_page()
            elif self.path == "/events":
                self._serve_events()
            elif self.path == "/api/status":
                self._serve_api()
            else:
                self.send_error(404)

        def _serve_page(self) -> None:
            body = _PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve_api(self) -> None:
            body = state.get_current_payload().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve_events(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            # Push initial current state immediately upon connection
            init_payload = state.get_current_payload()
            try:
                self.wfile.write(f"data: {init_payload}\n\n".encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

            q = state.subscribe()
            try:
                while True:
                    try:
                        data = q.get(timeout=15)
                        self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
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
# TV-Ready Responsive Grid Dashboard HTML
# ---------------------------------------------------------------------------

_PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Squawk</title>
<style>
  *, *::before, *::after {
    box-sizing: border-box;
    margin: 0;
    padding: 0;
  }

  :root {
    --bg-main: #0a0d14;
    --bg-card: rgba(17, 23, 34, 0.92);
    --border-card: rgba(56, 189, 248, 0.15);
    --accent-blue: #38bdf8;
    --accent-cyan: #22d3ee;
    --accent-amber: #f59e0b;
    --accent-emerald: #10b981;
    --accent-red: #ef4444;
    --text-primary: #f8fafc;
    --text-muted: #94a3b8;
    --text-dim: #475569;
  }

  body {
    background: var(--bg-main);
    color: var(--text-primary);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    min-height: 100vh;
    width: 100vw;
    overflow-x: hidden;
    padding: 1.5vw;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
  }

  /* Top Bar */
  header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding-bottom: 1.2vw;
    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
    margin-bottom: 1.2vw;
  }

  .brand {
    display: flex;
    align-items: center;
    gap: 0.8vw;
    font-size: clamp(1.2rem, 2vw, 2.4rem);
    font-weight: 900;
    letter-spacing: 0.12em;
    color: #fff;
    text-transform: uppercase;
  }

  /* Link indicator — driven by EventSource state, never decorative.
     Only the healthy state moves, so movement means something. */
  .radar-dot {
    width: clamp(10px, 1.2vw, 18px);
    height: clamp(10px, 1.2vw, 18px);
    background-color: var(--text-dim);
    border-radius: 50%;
    flex: none;
  }

  .radar-dot.link-ok {
    background-color: var(--accent-emerald);
    box-shadow: 0 0 12px var(--accent-emerald);
    animation: pulse 2s infinite ease-in-out;
  }

  .radar-dot.link-reconnecting {
    background-color: var(--accent-amber);
    box-shadow: 0 0 12px var(--accent-amber);
  }

  .radar-dot.link-down {
    background-color: var(--accent-red);
    box-shadow: 0 0 12px var(--accent-red);
  }

  .link-label {
    font-family: ui-monospace, "Cascadia Code", "Source Code Pro", monospace;
    font-size: clamp(0.7rem, 1vw, 1.2rem);
    font-weight: 700;
    letter-spacing: 0.1em;
    color: var(--text-muted);
  }

  .link-label.link-ok { color: var(--accent-emerald); }
  .link-label.link-reconnecting { color: var(--accent-amber); }
  .link-label.link-down { color: var(--accent-red); }

  @keyframes pulse {
    0%, 100% { transform: scale(1); opacity: 1; }
    50% { transform: scale(1.35); opacity: 0.5; }
  }

  .sys-info {
    display: flex;
    align-items: center;
    gap: 1.5vw;
    font-family: ui-monospace, "Cascadia Code", "Source Code Pro", monospace;
    font-size: clamp(0.9rem, 1.4vw, 1.6rem);
    color: var(--text-muted);
  }

  .clock {
    color: var(--accent-cyan);
    font-weight: 700;
  }

  /* Grid Layout for Panels — fixed 4x2, matching the physical wall.
     Slots hold their position whether or not a chain is assigned to them. */
  #dashboard {
    flex: 1;
    display: grid;
    gap: 1.5vw;
    width: 100%;
    grid-template-columns: repeat(4, 1fr);
    grid-template-rows: repeat(2, 1fr);
  }

  /* Section Card */
  .panel-card {
    background: var(--bg-card);
    border: 1px solid var(--border-card);
    border-radius: clamp(8px, 1.1vw, 16px);
    padding: 1vw 1.2vw;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    box-shadow: 0 12px 35px rgba(0, 0, 0, 0.65);
    position: relative;
    overflow: hidden;
  }

  .panel-card::before {
    content: "";
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    height: 4px;
    background: linear-gradient(90deg, var(--accent-blue), var(--accent-cyan));
  }

  /* Panel Header */
  .panel-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.8vw;
  }

  .panel-badge {
    font-size: clamp(0.75rem, 1.05vw, 1.35rem);
    font-weight: 800;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--accent-blue);
    background: rgba(56, 189, 248, 0.12);
    padding: 0.35vw 0.85vw;
    border-radius: 999px;
    border: 1px solid rgba(56, 189, 248, 0.3);
  }

  .panel-tag {
    font-size: clamp(0.7rem, 0.95vw, 1.2rem);
    font-weight: 600;
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }

  /* Aircraft Main Info */
  .aircraft-main {
    flex: 1;
    display: flex;
    flex-direction: column;
    justify-content: center;
    margin: 0.5vw 0;
  }

  .breed-line {
    font-size: clamp(1.1rem, 1.45vw, 2.2rem);
    font-weight: 800;
    color: #ffffff;
    line-height: 1.15;
    margin-bottom: 0.3vw;
  }

  .airline-tag {
    color: var(--accent-cyan);
    font-weight: 900;
  }

  .breed-tag {
    color: #ffffff;
    font-weight: 700;
  }

  .ident-row {
    display: flex;
    align-items: baseline;
    gap: 0.8vw;
    margin-bottom: 0.4vw;
  }

  .ident {
    font-size: clamp(1.2rem, 1.65vw, 2.4rem);
    font-weight: 700;
    letter-spacing: 0.04em;
    line-height: 1;
    color: var(--text-muted);
    font-family: ui-monospace, "Cascadia Code", monospace;
  }

  .vrate {
    font-size: clamp(1.1rem, 1.5vw, 2.2rem);
    font-weight: 700;
  }

  .vrate.up { color: var(--accent-emerald); }
  .vrate.down { color: var(--accent-amber); }
  .vrate.level { color: var(--text-dim); }

  .type-line {
    font-size: clamp(1.1rem, 1.7vw, 2.4rem);
    font-weight: 600;
    color: var(--text-muted);
    margin-bottom: 1vw;
  }

  .airline-tag {
    color: var(--accent-cyan);
    font-weight: 700;
  }

  /* Route Pill */
  .route-box {
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: clamp(8px, 1vw, 16px);
    padding: 0.5vw 0.8vw;
    margin: 0.5vw 0;
  }

  .route-airports {
    display: flex;
    align-items: center;
    gap: 1vw;
    font-size: clamp(0.95rem, 1.3vw, 1.9rem);
    font-weight: 800;
    color: #fff;
    font-family: ui-monospace, monospace;
  }

  .route-arrow {
    color: var(--accent-amber);
  }

  .route-names {
    font-size: clamp(0.75rem, 1vw, 1.3rem);
    color: var(--text-muted);
    margin-top: 0.3vw;
  }

  /* Metrics Grid */
  .metrics-grid {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 1vw;
    margin-top: 1.2vw;
    padding-top: 1.2vw;
    border-top: 1px solid rgba(255, 255, 255, 0.06);
  }

  .metric-item {
    display: flex;
    flex-direction: column;
  }

  .metric-label {
    font-size: clamp(0.65rem, 0.85vw, 1.1rem);
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--text-dim);
    margin-bottom: 0.2vw;
  }

  .metric-val {
    font-size: clamp(1rem, 1.35vw, 1.9rem);
    font-weight: 800;
    font-family: ui-monospace, "Cascadia Code", monospace;
    color: #fff;
  }

  .metric-val.alt {
    color: var(--accent-amber);
  }

  .metric-val.dist {
    color: var(--accent-cyan);
  }

  /* Empty State */
  .empty-state {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    color: var(--text-dim);
    text-align: center;
    gap: 0.8vw;
  }

  .empty-text {
    font-family: ui-monospace, "Cascadia Code", "Source Code Pro", monospace;
    font-size: clamp(0.9rem, 1.3vw, 1.7rem);
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  /* Unassigned slot — no chain is configured for this position.
     Distinct from a running chain that currently sees nothing (NO TARGET). */
  .slot-empty::before {
    display: none;
  }

  .slot-empty {
    background: rgba(17, 23, 34, 0.45);
    border-style: dashed;
    border-color: rgba(255, 255, 255, 0.06);
    justify-content: center;
  }

  .slot-number {
    font-family: ui-monospace, "Cascadia Code", "Source Code Pro", monospace;
    font-size: clamp(1.6rem, 3vw, 4rem);
    font-weight: 800;
    color: var(--text-dim);
    opacity: 0.5;
    line-height: 1;
  }

  /* A chain that has stopped updating. The panel stays in its slot — the wall
     never reflows — but stops looking like live data. */
  .panel-card.stale {
    opacity: 0.42;
    filter: saturate(0.4);
  }

  .panel-age {
    font-family: ui-monospace, "Cascadia Code", "Source Code Pro", monospace;
    font-size: clamp(0.6rem, 0.85vw, 1.05rem);
    color: var(--text-dim);
    letter-spacing: 0.04em;
    white-space: nowrap;
  }

  .panel-card.stale .panel-age {
    color: var(--accent-amber);
  }

  .panel-header-right {
    display: flex;
    align-items: center;
    gap: 0.6vw;
  }
</style>
</head>
<body>

<header>
  <div class="brand">
    <div class="radar-dot" id="link-dot"></div>
    <span class="link-label" id="link-label">CONNECTING</span>
    <span>Squawk Live Radar</span>
  </div>
  <div class="sys-info">
    <span id="monitoring">MONITORING — FLIGHTS</span>
    <span>•</span>
    <span id="channel-count">0 CHANNELS</span>
    <span>•</span>
    <span class="clock" id="clock">--:--:-- UTC</span>
  </div>
</header>

<main id="dashboard">
  <div class="panel-card slot-empty"><div class="empty-state"><div class="empty-text">Connecting</div></div></div>
</main>

<script>
const dashboard = document.getElementById('dashboard');
const clockElem = document.getElementById('clock');
const channelElem = document.getElementById('channel-count');
const monitoringElem = document.getElementById('monitoring');
const linkDot = document.getElementById('link-dot');
const linkLabel = document.getElementById('link-label');

const SLOTS = 8;

// The client is not told each chain's poll interval, so a chain that has gone
// quiet is judged against a flat threshold rather than three of its own cycles.
const STALE_AFTER_SECONDS = 30;

function esc(s) {
  if (s === null || s === undefined) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function updateClock() {
  const now = new Date();
  const utc = now.toUTCString().split(' ')[4] + ' UTC';
  clockElem.textContent = utc;
}
setInterval(updateClock, 1000);
updateClock();

function ageSeconds(panel) {
  if (!panel.updated_epoch) return null;
  return Math.max(0, Math.floor(Date.now() / 1000 - panel.updated_epoch));
}

function ageText(secs) {
  if (secs === null) return '';
  if (secs < 60) return secs + 's ago';
  const mins = Math.floor(secs / 60);
  if (mins < 60) return mins + 'm ago';
  return Math.floor(mins / 60) + 'h ago';
}

function renderEmptySlot(n) {
  return `
    <div class="panel-card slot-empty">
      <div class="empty-state">
        <div class="slot-number">${n}</div>
        <div class="empty-text">Unassigned</div>
      </div>
    </div>
  `;
}

function renderCard(panel) {
  const title = esc(panel.title || panel.chain_name || 'TRAFFIC');
  const list = panel.aircraft || [];
  const a = list.length > 0 ? list[0] : null;

  const secs = ageSeconds(panel);
  const stale = secs !== null && secs > STALE_AFTER_SECONDS;
  const cardClass = stale ? 'panel-card stale' : 'panel-card';
  const age = `<span class="panel-age">${esc(ageText(secs))}</span>`;

  if (!a) {
    return `
      <div class="${cardClass}">
        <div class="panel-header">
          <span class="panel-badge">${title}</span>
          <span class="panel-header-right">
            ${age}
            <span class="panel-tag">NO TARGET</span>
          </span>
        </div>
        <div class="empty-state">
          <div class="empty-text">No Target</div>
        </div>
        <div class="metrics-grid">
          <div class="metric-item">
            <span class="metric-label">Altitude</span>
            <span class="metric-val alt">—</span>
          </div>
          <div class="metric-item">
            <span class="metric-label">Distance</span>
            <span class="metric-val dist">—</span>
          </div>
        </div>
      </div>
    `;
  }

  const vrateClass = a.vrate === '↑' ? 'up' : a.vrate === '↓' ? 'down' : 'level';
  // Description first, designator as the fallback: the prose reads better on a
  // wall, but a code is far better than nothing.
  const typeLabel = esc(a.type_description || a.type_code || 'Unknown Airframe');

  let routeHtml = '';
  if (a.origin_iata || a.destination_iata) {
    const orig = esc(a.origin_iata || '?');
    const dest = esc(a.destination_iata || '?');
    const origC = a.origin_country ? esc(a.origin_country) : '';
    const destC = a.destination_country ? esc(a.destination_country) : '';
    const subRoute = (origC && destC) ? `${origC} → ${destC}` : '';

    routeHtml = `
      <div class="route-box">
        <div class="route-airports">
          <span>${orig}</span>
          <span class="route-arrow">✈</span>
          <span>${dest}</span>
        </div>
        ${subRoute ? `<div class="route-names">${subRoute}</div>` : ''}
      </div>
    `;
  }

  return `
    <div class="${cardClass}">
      <div class="panel-header">
        <span class="panel-badge">${title}</span>
        <span class="panel-header-right">
          ${age}
          <span class="panel-tag">${esc(a.callsign ? a.callsign : a.icao_hex)}</span>
        </span>
      </div>

      <div class="aircraft-main">
        <div class="breed-line">
          ${a.airline ? `<span class="airline-tag">${esc(a.airline)}</span> ` : ''}
          <span class="breed-tag">${typeLabel}</span>
        </div>
        <div class="ident-row">
          <span class="ident">${esc(a.ident)}</span>
          <span class="vrate ${vrateClass}">${esc(a.vrate)}</span>
        </div>
        ${routeHtml}
      </div>

      <div class="metrics-grid">
        <div class="metric-item">
          <span class="metric-label">Altitude</span>
          <span class="metric-val alt">${esc(a.altitude)}</span>
        </div>
        <div class="metric-item">
          <span class="metric-label">Distance</span>
          <span class="metric-val dist">${esc(a.distance)}</span>
        </div>
      </div>
    </div>
  `;
}

let lastState = null;

function renderPanels(state) {
  const panels = state.panels || {};
  const bySlot = {};
  for (const k of Object.keys(panels)) {
    const p = panels[k];
    if (p && p.slot >= 1 && p.slot <= SLOTS) {
      bySlot[p.slot] = p;
    }
  }

  // Always eight cards. A dead chain leaves a gap where it was, rather than
  // shuffling the seven survivors into new positions.
  let html = '';
  for (let slot = 1; slot <= SLOTS; slot++) {
    html += bySlot[slot] ? renderCard(bySlot[slot]) : renderEmptySlot(slot);
  }
  dashboard.innerHTML = html;
}

function renderState(state) {
  if (!state) return;
  lastState = state;

  if (state.timestamp) {
    clockElem.textContent = state.timestamp;
  }

  const tracked = (state.system || {}).tracked;
  monitoringElem.textContent = (tracked === undefined || tracked === null)
    ? 'MONITORING — FLIGHTS'
    : `MONITORING ${tracked} FLIGHTS`;

  const count = Object.keys(state.panels || {}).length;
  channelElem.textContent = `${count} ${count === 1 ? 'CHANNEL' : 'CHANNELS'}`;

  renderPanels(state);
}

// Re-render on a timer as well as on message, so panel ages keep counting and
// a chain that stops updating dims on its own without needing fresh data.
setInterval(function() {
  if (lastState) renderPanels(lastState);
}, 5000);

function setLink(cls, label) {
  linkDot.className = 'radar-dot ' + cls;
  linkLabel.className = 'link-label ' + cls;
  linkLabel.textContent = label;
}

const es = new EventSource('/events');

es.onopen = function() {
  setLink('link-ok', 'LINK OK');
};

es.onerror = function() {
  if (es.readyState === EventSource.CLOSED) {
    setLink('link-down', 'LINK DOWN');
  } else {
    setLink('link-reconnecting', 'RECONNECTING');
  }
};

es.onmessage = function(e) {
  try {
    const data = JSON.parse(e.data);
    renderState(data);
  } catch(err) {
    console.error('Error parsing SSE:', err);
  }
};

// Prevent screen from sleeping / dimming via Screen WakeLock API
if ('wakeLock' in navigator) {
  let wakeLock = null;
  const requestWakeLock = async () => {
    try {
      wakeLock = await navigator.wakeLock.request('screen');
    } catch(e) {}
  };
  requestWakeLock();
  document.addEventListener('visibilitychange', () => {
    if (wakeLock !== null && document.visibilityState === 'visible') {
      requestWakeLock();
    }
  });
}
</script>
</body>
</html>
"""
