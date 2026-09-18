#!/usr/bin/env python3
"""
Helvar showroom controller.

A grid of island photographs on a tablet. Tap a photo, that island's
luminaires come on. Tap it again, they go off.

Reads islands.json - either written by helvar_mapper.py or edited by hand.
Each island lists the DALI addresses it owns, group numbers, or both:

    {
      "router_ip": "192.168.1.50",
      "islands": [
        {"id": 1, "name": "Oak ceiling", "photo": "photos/island-1.jpg",
         "addresses": ["@1.1.1.1", "@1.1.1.2"]},
        {"id": 2, "name": "Brass pendants", "groups": [7], "on_level": 80}
      ]
    }

Because islands can be driven by address, no DALI groups need to exist and
no Designer work is required.

    python3 helvar_controller.py

Single dependency-free process; put the island photos in ./photos/.
"""

import http.server
import json
import mimetypes
import os
import socket
import socketserver
import threading
import urllib.parse

from helvarnet import HelvarClient

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "controller_config.json")
ISLANDS_PATH = os.path.join(HERE, "islands.json")
STATE_PATH = os.path.join(HERE, "controller_state.json")

DEFAULT_CONFIG = {
    "router_ip": "192.168.1.50",
    "router_port": 50000,
    "http_port": 8080,
    "command_timeout": 1.0,

    "on_level": 100,
    "fade": 100,          # hundredths of a second, so 100 = 1.0s
    "photos_dir": "photos",
    "title": "Showroom",

    "_logo_comment": (
        "Path to a logo image, relative to this file. Shown in the header in "
        "place of the title text; SVG keeps crisp on a high-DPI tablet. The "
        "title is still used for the browser tab and as the image's alt text. "
        "If the file is missing the header falls back to the title."
    ),
    "logo": "logo.svg",
    "logo_height": "30px",

    "_theme_comment": (
        "Brand palette. Every colour the page uses is here - nothing is "
        "hard-coded in the stylesheet. 'accent' is the lit-island colour, "
        "'accent_ink' the text drawn on top of it, 'accent_soft' the glow "
        "around a lit tile (use a translucent rgba)."
    ),
    "theme": {
        # Lemora. The accent is the brand turquoise - the brandbook artwork
        # specifies it as C81 M0 Y39 K0; this is its screen equivalent.
        # The greys follow the wordmark's neutral scale.
        "bg": "#16191b",
        "panel": "#212527",
        "line": "#343a3d",
        "text": "#eef0f1",
        "muted": "#8a9094",
        "accent": "#00b2ac",
        "accent_ink": "#04201f",
        "accent_soft": "rgba(0, 178, 172, .30)",
        "danger": "#e5372b",
        "font": ('system-ui, -apple-system, "Segoe UI", Roboto, sans-serif'),
        "radius": "8px",

        "tile_bg": "#1c2022",
        "photo_text": "#ffffff",
        "photo_muted": "rgba(255, 255, 255, .22)",
    },

    "_comment": (
        "Command numbers follow the HelvarNet overview document. If tapping a "
        "tile does nothing, check these against the copy for your firmware "
        "using the raw command box in the mapper."
    ),
    "cmd_direct_level_device": 14,
    "cmd_direct_level_group": 13,
}

SAMPLE_ISLANDS = {
    "router_ip": "192.168.1.50",
    "router_port": 50000,
    "islands": [
        {"id": 1, "name": "Island 1", "photo": "photos/island-1.jpg",
         "addresses": []},
    ],
}


def load_json(path, default, label):
    if not os.path.exists(path):
        with open(path, "w") as fh:
            json.dump(default, fh, indent=2)
        print(f"Created {path} - {label}")
        return json.loads(json.dumps(default))
    with open(path) as fh:
        return json.load(fh)


CONFIG = dict(DEFAULT_CONFIG)
_user_config = load_json(CONFIG_PATH, DEFAULT_CONFIG, "set router_ip, then restart")
_user_theme = dict(DEFAULT_CONFIG["theme"])
_user_theme.update(_user_config.get("theme") or {})
CONFIG.update(_user_config)
CONFIG["theme"] = _user_theme

ISLANDS_DOC = load_json(
    ISLANDS_PATH, SAMPLE_ISLANDS,
    "run helvar_mapper.py to fill this in, or edit it by hand")

# islands.json carries the router it was mapped against; let it win, so the
# two files cannot silently disagree about which router is being driven.
ROUTER_IP = ISLANDS_DOC.get("router_ip") or CONFIG["router_ip"]
ROUTER_PORT = ISLANDS_DOC.get("router_port") or CONFIG["router_port"]

CLIENT = HelvarClient(
    ROUTER_IP, ROUTER_PORT, CONFIG["command_timeout"],
    commands={
        "direct_level_device": CONFIG["cmd_direct_level_device"],
        "direct_level_group": CONFIG["cmd_direct_level_group"],
    },
)

PHOTOS_DIR = os.path.join(HERE, CONFIG["photos_dir"])


def logo_path():
    """Absolute path of the configured logo, or None if absent or outside."""
    rel = (CONFIG.get("logo") or "").strip()
    if not rel:
        return None
    candidate = os.path.abspath(os.path.join(HERE, rel))
    if not candidate.startswith(os.path.abspath(HERE) + os.sep):
        return None
    return candidate if os.path.isfile(candidate) else None


LOGO_PATH = logo_path()


# --------------------------------------------------------------------------
# Island model
# --------------------------------------------------------------------------

class Island:
    def __init__(self, raw):
        self.id = int(raw["id"])
        self.name = raw.get("name") or f"Island {self.id}"
        self.photo = raw.get("photo") or f"photos/island-{self.id}.jpg"
        self.addresses = list(raw.get("addresses") or [])
        self.groups = list(raw.get("groups") or [])
        self.on_level = int(raw.get("on_level", CONFIG["on_level"]))

    @property
    def target_count(self):
        return len(self.addresses) + len(self.groups)

    def photo_path(self):
        """Absolute path of the photo, or None if it is missing or escapes."""
        rel = self.photo
        if rel.startswith("photos/"):
            rel = rel[len("photos/"):]
        candidate = os.path.abspath(os.path.join(PHOTOS_DIR, rel))
        if not candidate.startswith(os.path.abspath(PHOTOS_DIR) + os.sep):
            return None
        return candidate if os.path.isfile(candidate) else None

    def set_level(self, level, fade):
        """Drive every target this island owns. Groups first - they are one
        command each, so the room reacts sooner when both are present."""
        for group in self.groups:
            CLIENT.direct_level_group(group, level, fade)
        for address in self.addresses:
            CLIENT.direct_level_device(address, level, fade)


def load_islands():
    return [Island(raw) for raw in ISLANDS_DOC.get("islands", [])]


ISLANDS = load_islands()
ISLANDS_BY_ID = {i.id: i for i in ISLANDS}


# --------------------------------------------------------------------------
# On/off state
#
# The tablet is the only control in the showroom, so what we last sent is the
# truth. No polling, no push messages. If somebody also switches an island
# from a wall panel, this will drift - press All off to resynchronise.
# --------------------------------------------------------------------------

STATE_LOCK = threading.Lock()
STATE = {"on": [], "solo": False}

if os.path.exists(STATE_PATH):
    try:
        with open(STATE_PATH) as fh:
            saved = json.load(fh)
        STATE["on"] = [int(i) for i in saved.get("on", []) if int(i) in ISLANDS_BY_ID]
        STATE["solo"] = bool(saved.get("solo", False))
    except (OSError, ValueError) as exc:
        print(f"Could not read {STATE_PATH} ({exc}); starting with all off.")


def save_state():
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(STATE, fh, indent=2)
    os.replace(tmp, STATE_PATH)


def build_state_payload():
    with STATE_LOCK:
        on = set(STATE["on"])
        solo = STATE["solo"]
    return {
        "title": CONFIG["title"],
        "has_logo": LOGO_PATH is not None,
        "router_ip": ROUTER_IP,
        "solo": solo,
        "islands": [
            {
                "id": i.id,
                "name": i.name,
                "on": i.id in on,
                "has_photo": i.photo_path() is not None,
                "targets": i.target_count,
            }
            for i in ISLANDS
        ],
        "on_count": len(on),
        "total": len(ISLANDS),
        "switchable": sum(1 for i in ISLANDS if i.target_count > 0),
        "last_error": CLIENT.last_error,
    }


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------

def api_toggle(body):
    island_id = body.get("id")
    if island_id is None:
        return {"ok": False, "error": "id required"}
    island = ISLANDS_BY_ID.get(int(island_id))
    if island is None:
        return {"ok": False, "error": f"no island {island_id}"}
    if island.target_count == 0:
        # Not every island in the showroom has controllable lighting. Such a
        # tile is a display, not a fault - tapping it simply does nothing.
        return {"ok": True, "on": False}

    fade = CONFIG["fade"]
    with STATE_LOCK:
        on = set(STATE["on"])
        solo = STATE["solo"]
        turning_on = island.id not in on

        if solo and turning_on:
            others = [ISLANDS_BY_ID[i] for i in on if i in ISLANDS_BY_ID]
            new_on = {island.id}
        else:
            others = []
            new_on = on | {island.id} if turning_on else on - {island.id}

        STATE["on"] = sorted(new_on)
        save_state()

    # talk to the router outside the lock - a slow socket must not block the
    # other tablets polling for state
    for other in others:
        other.set_level(0, fade)
    island.set_level(island.on_level if turning_on else 0, fade)
    return {"ok": True, "on": turning_on}


def api_all(body):
    want_on = bool(body.get("on"))
    fade = CONFIG["fade"]
    targets = [i for i in ISLANDS if i.target_count > 0]
    with STATE_LOCK:
        STATE["on"] = sorted(i.id for i in targets) if want_on else []
        save_state()
    errors = 0
    for island in targets:
        try:
            island.set_level(island.on_level if want_on else 0, fade)
        except (OSError, ConnectionError):
            errors += 1
    return {"ok": errors == 0, "count": len(targets), "errors": errors}


def api_solo(body):
    """Solo mode: turning an island on turns every other island off.

    The showroom gesture - a customer asks about one fixture and you isolate
    it without tapping thirty-eight others off.
    """
    with STATE_LOCK:
        STATE["solo"] = bool(body.get("solo"))
        solo = STATE["solo"]
        extra = [ISLANDS_BY_ID[i] for i in STATE["on"][1:]] if solo else []
        if solo and len(STATE["on"]) > 1:
            STATE["on"] = STATE["on"][:1]
        save_state()
    for island in extra:
        island.set_level(0, CONFIG["fade"])
    return {"ok": True, "solo": solo}


def api_reload(body):
    """Re-read islands.json without restarting - handy while mapping."""
    global ISLANDS_DOC, ISLANDS, ISLANDS_BY_ID
    try:
        with open(ISLANDS_PATH) as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"islands.json: {exc}"}
    ISLANDS_DOC = doc
    ISLANDS = load_islands()
    ISLANDS_BY_ID = {i.id: i for i in ISLANDS}
    with STATE_LOCK:
        STATE["on"] = [i for i in STATE["on"] if i in ISLANDS_BY_ID]
        save_state()
    return {"ok": True, "count": len(ISLANDS)}


ROUTES = {
    "/api/toggle": api_toggle,
    "/api/all": api_all,
    "/api/solo": api_solo,
    "/api/reload": api_reload,
}


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "HelvarController/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, content_type="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _send_photo(self, island_id):
        island = ISLANDS_BY_ID.get(island_id)
        path = island.photo_path() if island else None
        if path is None:
            self._send(404, {"error": "no photo"})
            return
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            self._send(404, {"error": "unreadable"})
            return
        # photos change rarely; let the tablet cache them between sessions
        self._send(200, data, ctype, {"Cache-Control": "public, max-age=86400"})

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8",
                       {"Cache-Control": "no-store"})
        elif path == "/api/state":
            self._send(200, build_state_payload(), extra={"Cache-Control": "no-store"})
        elif path.startswith("/photo/"):
            try:
                self._send_photo(int(path[len("/photo/"):]))
            except ValueError:
                self._send(404, {"error": "bad id"})
        elif path == "/logo":
            if LOGO_PATH is None:
                self._send(404, {"error": "no logo"})
                return
            ctype = mimetypes.guess_type(LOGO_PATH)[0] or "image/svg+xml"
            try:
                with open(LOGO_PATH, "rb") as fh:
                    data = fh.read()
            except OSError:
                self._send(404, {"error": "unreadable"})
                return
            self._send(200, data, ctype,
                       {"Cache-Control": "public, max-age=86400"})
        elif path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length)) if length else {}
        except ValueError:
            body = {}
        handler = ROUTES.get(path)
        if handler is None:
            self._send(404, {"error": "not found"})
            return
        try:
            result = handler(body)
        except (OSError, ConnectionError) as exc:
            result = {"ok": False, "error": f"router unreachable: {exc}"}
        except Exception as exc:
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        self._send(200, result if result is not None else {"ok": True})


# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<title>Showroom</title>
<style>
  /* every colour comes from the "theme" block in controller_config.json */
  :root {
    --bg: __BG__;
    --panel: __PANEL__;
    --line: __LINE__;
    --text: __TEXT__;
    --muted: __MUTED__;
    --accent: __ACCENT__;
    --accent-ink: __ACCENT_INK__;
    --accent-soft: __ACCENT_SOFT__;
    --danger: __DANGER__;
    --radius: __RADIUS__;
    --tile-bg: __TILE_BG__;
    --photo-text: __PHOTO_TEXT__;
    --photo-muted: __PHOTO_MUTED__;
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html, body { overscroll-behavior: none; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 16px/1.4 __FONT__;
    padding: 0 16px 32px; user-select: none;
  }

  header {
    display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
    padding: 14px 0 12px; position: sticky; top: 0; z-index: 5;
    background: linear-gradient(var(--bg) 78%, rgba(16,18,22,0));
  }
  h1 { font-size: 18px; margin: 0; font-weight: 600; }
  #logo { height: __LOGO_HEIGHT__; width: auto; display: block; }
  .spacer { flex: 1 1 auto; }
  .count {
    font-size: 13px; color: var(--muted); font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }
  .count b { color: var(--accent); font-weight: 650; }
  button {
    font: inherit; font-size: 15px; color: var(--text); background: var(--panel);
    border: 1px solid var(--line); border-radius: 10px;
    padding: 11px 16px; min-height: 46px; cursor: pointer;
  }
  button:active { transform: translateY(1px); }
  button.on { background: var(--accent); color: var(--accent-ink); border-color: var(--accent); font-weight: 650; }

  .grid {
    display: grid; gap: 12px;
    grid-template-columns: repeat(auto-fill, minmax(168px, 1fr));
  }
  .tile {
    position: relative; aspect-ratio: 4 / 3; border-radius: var(--radius);
    overflow: hidden; cursor: pointer; background: var(--tile-bg);
    border: 1px solid var(--line);
    transition: box-shadow .22s, border-color .22s, transform .1s;
  }
  .tile:active { transform: scale(.975); }
  .tile img {
    position: absolute; inset: 0; width: 100%; height: 100%;
    object-fit: cover; display: block;
    /* dark enough to read as off, bright enough that the photo is still
       recognisable across the room - tune to taste for your images */
    filter: brightness(.5) saturate(.5);
    transition: filter .3s ease;
  }
  .tile .ph {
    position: absolute; inset: 0; display: flex;
    align-items: center; justify-content: center;
    font-size: 40px; font-weight: 700; color: var(--photo-muted);
    background: var(--tile-bg);
    transition: color .3s ease;
  }
  .tile .label {
    position: absolute; left: 0; right: 0; bottom: 0; padding: 26px 12px 10px;
    font-size: 14px; font-weight: 600; line-height: 1.25; color: var(--photo-text);
    background: linear-gradient(transparent, rgba(8,9,12,.88));
    text-shadow: 0 1px 3px rgba(0,0,0,.7);
  }
  .tile.lit { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent), 0 6px 26px var(--accent-soft); }
  .tile.lit img { filter: none; }
  .tile.lit .ph { color: var(--accent); }

  .banner {
    display: none; margin: 0 0 12px; padding: 11px 14px; border-radius: 10px;
    background: #2a1614; border: 1px solid #5a2a27; color: #ffc9c5; font-size: 14px;
  }
  .banner.show { display: block; }
  .toast {
    position: fixed; left: 50%; transform: translateX(-50%); bottom: 22px;
    background: var(--panel); border: 1px solid var(--line);
    padding: 12px 18px; border-radius: 10px; font-size: 14px;
    opacity: 0; pointer-events: none; transition: opacity .2s; z-index: 20;
    max-width: 90vw;
  }
  .toast.on { opacity: 1; }
  .toast.err { border-color: var(--danger); color: #ffc9c5; }
  @media (max-width: 520px) {
    .grid { grid-template-columns: repeat(auto-fill, minmax(132px, 1fr)); gap: 10px; }
    h1 { font-size: 16px; }
  }
</style>
</head>
<body>

<header>
  <img id="logo" alt="" hidden>
  <h1 id="title" hidden></h1>
  <span class="count" id="count"></span>
  <span class="spacer"></span>
  <button id="soloBtn">Solo</button>
  <button id="allOnBtn">All on</button>
  <button id="allOffBtn">All off</button>
</header>

<div class="banner" id="banner"></div>
<div class="grid" id="grid"></div>
<div class="toast" id="toast"></div>

<script>
var S = null;
var pending = {};   // id -> the state we optimistically showed

function toast(msg, isError) {
  var el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'toast on' + (isError ? ' err' : '');
  clearTimeout(el._t);
  el._t = setTimeout(function () { el.className = 'toast'; }, 2600);
}

function post(path, body) {
  return fetch(path, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body || {})
  }).then(function (r) { return r.json(); }).then(function (j) {
    if (j && j.ok === false && j.error) toast(j.error, true);
    return j;
  }).catch(function (e) { toast('server unreachable', true); });
}

function refresh() {
  return fetch('/api/state').then(function (r) { return r.json(); })
    .then(function (s) { S = s; render(); })
    .catch(function () {});
}

function render() {
  if (!S) return;
  var logo = document.getElementById('logo');
  var title = document.getElementById('title');
  if (S.has_logo) {
    if (!logo.src) logo.src = '/logo';
    logo.alt = S.title;
    logo.hidden = false;
    title.hidden = true;
  } else {
    title.textContent = S.title;
    title.hidden = false;
    logo.hidden = true;
  }
  document.title = S.title;
  document.getElementById('count').innerHTML =
    '<b>' + S.on_count + '</b> of ' + S.total + ' lit';
  document.getElementById('soloBtn').className = S.solo ? 'on' : '';

  var banner = document.getElementById('banner');
  if (S.last_error) {
    banner.className = 'banner show';
    banner.textContent = 'Router ' + S.router_ip + ' is not responding. ' +
                         'Taps are being recorded but the lights will not move.';
  } else {
    banner.className = 'banner';
  }

  var grid = document.getElementById('grid');
  var sig = S.islands.map(function (i) {
    return i.id + ':' + i.name + ':' + i.has_photo + ':' + i.targets;
  }).join('|');

  if (sig !== grid._sig) {
    grid._sig = sig;
    grid._tiles = {};
    grid.innerHTML = '';
    S.islands.forEach(function (isl) {
      var t = document.createElement('div');
      t.className = 'tile';
      var inner = '';
      if (isl.has_photo) {
        inner += '<img alt="" src="/photo/' + isl.id + '">';
      } else {
        inner += '<div class="ph">' + isl.id + '</div>';
      }
      inner += '<div class="label"></div>';
      t.innerHTML = inner;
      t.querySelector('.label').textContent = isl.name;
      t.onclick = function () { tapIsland(isl); };
      grid.appendChild(t);
      grid._tiles[isl.id] = t;
    });
  }

  S.islands.forEach(function (isl) {
    var t = grid._tiles[isl.id];
    if (!t) return;
    // an in-flight tap wins until the server confirms, so the tile never
    // flickers back to its old state while the request is on the wire
    var lit = (isl.id in pending) ? pending[isl.id] : isl.on;
    t.className = 'tile' + (lit ? ' lit' : '');
  });
}

function tapIsland(isl) {
  if (!isl.targets) return;   // a display island with no controllable lighting
  var currently = (isl.id in pending) ? pending[isl.id] : isl.on;
  var next = !currently;

  if (S.solo && next) {
    // reflect the whole solo switch at once, not just this tile
    S.islands.forEach(function (o) { pending[o.id] = (o.id === isl.id); });
  } else {
    pending[isl.id] = next;
  }
  render();

  post('/api/toggle', {id: isl.id}).then(function () {
    pending = {};
    refresh();
  });
}

function setAll(on) {
  S.islands.forEach(function (o) { pending[o.id] = on && !!o.targets; });
  render();
  post('/api/all', {on: on}).then(function () { pending = {}; refresh(); });
}

function toggleSolo() {
  post('/api/solo', {solo: !S.solo}).then(function () { pending = {}; refresh(); });
}

document.getElementById('allOnBtn').onclick = function () { setAll(true); };
document.getElementById('allOffBtn').onclick = function () { setAll(false); };
document.getElementById('soloBtn').onclick = toggleSolo;

// keep the screen awake where the browser allows it; Fully Kiosk or the
// tablet's own display timeout covers the rest
function keepAwake() {
  if (!navigator.wakeLock) return;
  navigator.wakeLock.request('screen').catch(function () {});
}
document.addEventListener('visibilitychange', function () {
  if (!document.hidden) { keepAwake(); refresh(); }
});

keepAwake();
refresh();
setInterval(refresh, 2500);
</script>
</body>
</html>
"""


def render_page():
    """Substitute the configured palette into the stylesheet."""
    theme = CONFIG["theme"]
    page = PAGE
    for key in ("bg", "panel", "line", "text", "muted", "accent",
                "accent_ink", "accent_soft", "danger", "font", "radius",
                "tile_bg", "photo_text", "photo_muted"):
        page = page.replace("__%s__" % key.upper(), str(theme[key]))
    return page.replace("__LOGO_HEIGHT__", str(CONFIG["logo_height"]))


PAGE = render_page()


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    os.makedirs(PHOTOS_DIR, exist_ok=True)
    switchable = sum(1 for i in ISLANDS if i.target_count > 0)
    with_photo = sum(1 for i in ISLANDS if i.photo_path() is not None)

    print("Helvar controller")
    print(f"  router   {ROUTER_IP}:{ROUTER_PORT}")
    print(f"  islands  {len(ISLANDS)} "
          f"({switchable} switchable, {with_photo} with photos)")
    print(f"  photos   {PHOTOS_DIR}")
    print(f"  logo     {LOGO_PATH or 'none - showing the title text instead'}")
    try:
        host_ip = socket.gethostbyname(socket.gethostname())
    except OSError:
        host_ip = "this-machine"
    print(f"\n  open  http://{host_ip}:{CONFIG['http_port']}/  on the tablet\n")

    server = Server(("0.0.0.0", CONFIG["http_port"]), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        CLIENT.close()
        server.server_close()


if __name__ == "__main__":
    main()
