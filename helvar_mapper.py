#!/usr/bin/env python3
"""
Helvar DALI address mapper.

Walks the showroom with you and works out which DALI address drives which
physical luminaire, then which luminaires belong to which island.

Workflow:
  1. Scan   - probes every address on every subnet, keeps the ones that answer.
  2. Walk   - blinks one address at a time; you tap the island it belongs to.
  3. Export - writes islands.json, which the scene panel consumes later.

Single file, standard library only. Runs on a Raspberry Pi; drive it from a
tablet browser on the same network.

    python3 helvar_mapper.py

State is saved after every action, so you can stop mid-walk and pick it up
again without losing the mapping.
"""

import http.server
import json
import os
import socket
import socketserver
import threading
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "mapper_config.json")
STATE_PATH = os.path.join(HERE, "mapper_state.json")
EXPORT_PATH = os.path.join(HERE, "islands.json")

DEFAULT_CONFIG = {
    "router_ip": "192.168.1.50",
    "router_port": 50000,
    "cluster": 1,
    "router": 1,
    "subnets": [1, 2, 3, 4],
    "max_device_address": 64,
    "http_port": 8080,
    "probe_timeout": 0.4,
    "command_timeout": 1.0,

    "_comment": (
        "Command numbers below follow the HelvarNet overview document. Check "
        "them against the copy for your firmware before a long scan - if the "
        "scan finds nothing, a wrong query number is the first thing to "
        "suspect, not the wiring."
    ),
    "cmd_direct_level_device": 14,
    "cmd_direct_level_group": 13,
    "cmd_query_device_type": 103,
    "cmd_query_device_description": 105,
}


def load_config():
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w") as fh:
            json.dump(DEFAULT_CONFIG, fh, indent=2)
        print(f"Created {CONFIG_PATH} - set router_ip, then restart.")
        return dict(DEFAULT_CONFIG)
    with open(CONFIG_PATH) as fh:
        cfg = json.load(fh)
    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg)
    return merged


CONFIG = load_config()


# --------------------------------------------------------------------------
# HelvarNet transport
# --------------------------------------------------------------------------

class HelvarClient:
    """One persistent TCP connection to the router, serialised by a lock.

    Routers accept a limited number of concurrent connections, so we keep a
    single socket open and reconnect on failure rather than dialling per
    command.
    """

    def __init__(self, ip, port, command_timeout):
        self.ip = ip
        self.port = port
        self.command_timeout = command_timeout
        self._sock = None
        self._buf = b""
        self._lock = threading.Lock()
        self.last_error = None

    def _connect(self):
        self._close()
        sock = socket.create_connection((self.ip, self.port), timeout=3.0)
        sock.settimeout(self.command_timeout)
        self._sock = sock
        self._buf = b""

    def _close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self._buf = b""

    def _read_reply(self, timeout):
        """Read until a terminated message arrives, skipping router pushes.

        Replies end with '#'. A router with push messages enabled also sends
        unsolicited '>' commands down the same socket; those are not answers
        to anything we asked, so they get discarded.
        """
        deadline = time.monotonic() + timeout
        while True:
            idx = self._buf.find(b"#")
            if idx != -1:
                msg = self._buf[: idx + 1]
                self._buf = self._buf[idx + 1:]
                text = msg.decode("ascii", "replace").strip()
                if text.startswith(">"):
                    continue  # unsolicited push, not our reply
                return text

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self._sock.settimeout(remaining)
            try:
                chunk = self._sock.recv(4096)
            except socket.timeout:
                return None
            if not chunk:
                raise ConnectionError("router closed the connection")
            self._buf += chunk

    def send(self, command, expect_reply=False, timeout=None):
        """Send a raw HelvarNet string. Returns the reply text, or None."""
        if timeout is None:
            timeout = self.command_timeout
        if not command.endswith("#"):
            command += "#"

        with self._lock:
            for attempt in (1, 2):
                try:
                    if self._sock is None:
                        self._connect()
                    self._sock.sendall(command.encode("ascii"))
                    if not expect_reply:
                        self.last_error = None
                        return None
                    reply = self._read_reply(timeout)
                    self.last_error = None
                    return reply
                except (OSError, ConnectionError) as exc:
                    self._close()
                    if attempt == 2:
                        self.last_error = f"{type(exc).__name__}: {exc}"
                        raise
        return None

    # -- convenience wrappers ---------------------------------------------

    def direct_level_device(self, address, level, fade=0):
        cmd = ">V:1,C:%d,L:%d,F:%d,%s" % (
            CONFIG["cmd_direct_level_device"], level, fade, address)
        self.send(cmd)

    def query_device(self, address, command_number, timeout):
        cmd = ">V:1,C:%d,%s" % (command_number, address)
        return self.send(cmd, expect_reply=True, timeout=timeout)


CLIENT = HelvarClient(
    CONFIG["router_ip"], CONFIG["router_port"], CONFIG["command_timeout"])


def parse_reply(reply):
    """Split a HelvarNet reply into (ok, value).

    '?' prefixes an answer, '!' an error. The payload follows '='.
    """
    if not reply:
        return False, None
    ok = reply.startswith("?")
    value = None
    if "=" in reply:
        value = reply.split("=", 1)[1].rstrip("#").strip()
    return ok, value


# --------------------------------------------------------------------------
# Persistent state
# --------------------------------------------------------------------------

DEFAULT_STATE = {
    "devices": [],        # [{"address", "type", "description"}]
    "islands": [],        # [{"id", "name"}]
    "assignments": {},    # address -> island id
    "skipped": [],        # addresses explicitly passed over
    "cursor": 0,          # index into devices for the walk
    "scan": {"running": False, "done": 0, "total": 0, "found": 0, "error": None},
}

STATE_LOCK = threading.Lock()


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as fh:
                saved = json.load(fh)
            state = json.loads(json.dumps(DEFAULT_STATE))
            state.update(saved)
            state["scan"] = dict(DEFAULT_STATE["scan"])  # never resume a scan
            return state
        except (OSError, ValueError) as exc:
            print(f"Could not read {STATE_PATH} ({exc}); starting fresh.")
    return json.loads(json.dumps(DEFAULT_STATE))


STATE = load_state()


def save_state():
    """Write state atomically - an afternoon of walking is worth protecting."""
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(STATE, fh, indent=2)
    os.replace(tmp, STATE_PATH)


# --------------------------------------------------------------------------
# Blinker - keeps one luminaire pulsing so you can spot it across the floor
# --------------------------------------------------------------------------

class Blinker:
    def __init__(self, client):
        self.client = client
        self.address = None
        self.steady = False
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def start(self, address, steady=False):
        with self._lock:
            self._stop_locked()
            self.address = address
            self.steady = steady
            self._stop = threading.Event()
            self._thread = threading.Thread(
                target=self._run, args=(address, steady, self._stop), daemon=True)
            self._thread.start()

    def stop(self):
        with self._lock:
            self._stop_locked()

    def _stop_locked(self):
        if self._thread is not None:
            self._stop.set()
            self._thread.join(timeout=2.0)
            self._thread = None
        if self.address is not None:
            try:
                self.client.direct_level_device(self.address, 0)
            except (OSError, ConnectionError):
                pass
        self.address = None

    def _run(self, address, steady, stop_event):
        try:
            if steady:
                self.client.direct_level_device(address, 100)
                stop_event.wait()
                return
            while not stop_event.is_set():
                self.client.direct_level_device(address, 100)
                if stop_event.wait(0.7):
                    break
                self.client.direct_level_device(address, 0)
                if stop_event.wait(0.45):
                    break
        except (OSError, ConnectionError) as exc:
            print(f"blink stopped: {exc}")


BLINKER = Blinker(CLIENT)


# --------------------------------------------------------------------------
# Scanning
# --------------------------------------------------------------------------

def scan_worker():
    cluster = CONFIG["cluster"]
    router = CONFIG["router"]
    subnets = CONFIG["subnets"]
    max_addr = CONFIG["max_device_address"]
    timeout = CONFIG["probe_timeout"]

    total = len(subnets) * max_addr
    found = []

    with STATE_LOCK:
        STATE["scan"] = {"running": True, "done": 0, "total": total,
                         "found": 0, "error": None}

    try:
        for subnet in subnets:
            for device in range(1, max_addr + 1):
                address = "@%d.%d.%d.%d" % (cluster, router, subnet, device)
                present = False
                dev_type = None
                try:
                    reply = CLIENT.query_device(
                        address, CONFIG["cmd_query_device_type"], timeout)
                    ok, value = parse_reply(reply)
                    if ok and value:
                        present = True
                        dev_type = value
                except (OSError, ConnectionError) as exc:
                    with STATE_LOCK:
                        STATE["scan"]["running"] = False
                        STATE["scan"]["error"] = str(exc)
                    return

                description = ""
                if present:
                    try:
                        reply = CLIENT.query_device(
                            address, CONFIG["cmd_query_device_description"],
                            timeout)
                        ok, value = parse_reply(reply)
                        if ok and value:
                            description = value
                    except (OSError, ConnectionError):
                        pass
                    found.append({"address": address, "type": dev_type,
                                  "description": description})

                with STATE_LOCK:
                    STATE["scan"]["done"] += 1
                    STATE["scan"]["found"] = len(found)

        with STATE_LOCK:
            known = {d["address"] for d in STATE["devices"]}
            for entry in found:
                if entry["address"] not in known:
                    STATE["devices"].append(entry)
            STATE["devices"].sort(key=lambda d: address_sort_key(d["address"]))
            STATE["scan"]["running"] = False
            save_state()
    except Exception as exc:  # keep the server alive whatever the router does
        with STATE_LOCK:
            STATE["scan"]["running"] = False
            STATE["scan"]["error"] = f"{type(exc).__name__}: {exc}"


def address_sort_key(address):
    try:
        return tuple(int(part) for part in address.lstrip("@").split("."))
    except ValueError:
        return (0, 0, 0, 0)


# --------------------------------------------------------------------------
# Walk helpers
# --------------------------------------------------------------------------

def unmapped_indexes():
    """Indexes of devices with no island and no explicit skip."""
    skipped = set(STATE["skipped"])
    return [i for i, dev in enumerate(STATE["devices"])
            if dev["address"] not in STATE["assignments"]
            and dev["address"] not in skipped]


def clamp_cursor():
    if not STATE["devices"]:
        STATE["cursor"] = 0
        return
    STATE["cursor"] = max(0, min(STATE["cursor"], len(STATE["devices"]) - 1))


def advance_to_next_unmapped():
    """Move the cursor forward to the next device still needing an island."""
    total = len(STATE["devices"])
    if total == 0:
        return
    skipped = set(STATE["skipped"])
    for step in range(1, total + 1):
        idx = (STATE["cursor"] + step) % total
        dev = STATE["devices"][idx]
        if (dev["address"] not in STATE["assignments"]
                and dev["address"] not in skipped):
            STATE["cursor"] = idx
            return
    STATE["cursor"] = min(STATE["cursor"] + 1, total - 1)


def current_device():
    clamp_cursor()
    if not STATE["devices"]:
        return None
    return STATE["devices"][STATE["cursor"]]


def build_state_payload():
    with STATE_LOCK:
        clamp_cursor()
        device = STATE["devices"][STATE["cursor"]] if STATE["devices"] else None
        counts = {}
        for island_id in STATE["assignments"].values():
            counts[str(island_id)] = counts.get(str(island_id), 0) + 1
        return {
            "router_ip": CONFIG["router_ip"],
            "devices": STATE["devices"],
            "islands": STATE["islands"],
            "assignments": STATE["assignments"],
            "skipped": STATE["skipped"],
            "cursor": STATE["cursor"],
            "current": device,
            "counts": counts,
            "remaining": len(unmapped_indexes()),
            "scan": STATE["scan"],
            "blinking": BLINKER.address,
            "steady": BLINKER.steady,
            "last_error": CLIENT.last_error,
        }


def export_islands():
    with STATE_LOCK:
        by_island = {}
        for address, island_id in STATE["assignments"].items():
            by_island.setdefault(int(island_id), []).append(address)
        islands = []
        for island in STATE["islands"]:
            addresses = sorted(by_island.get(island["id"], []),
                               key=address_sort_key)
            islands.append({
                "id": island["id"],
                "name": island["name"],
                "photo": "photos/island-%d.jpg" % island["id"],
                "addresses": addresses,
            })
        payload = {
            "router_ip": CONFIG["router_ip"],
            "router_port": CONFIG["router_port"],
            "islands": islands,
        }
    with open(EXPORT_PATH, "w") as fh:
        json.dump(payload, fh, indent=2)
    return payload


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "HelvarMapper/1.0"

    def log_message(self, fmt, *args):
        pass  # the console is for router errors, not request noise

    def _send(self, code, body, content_type="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except ValueError:
            return dict(urllib.parse.parse_qsl(raw.decode("utf-8")))

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif path == "/api/state":
            self._send(200, build_state_payload())
        elif path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path == "/api/export":
            payload = export_islands()
            self._send(200, payload)
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        body = self._body()
        try:
            handler = ROUTES.get(path)
            if handler is None:
                self._send(404, {"error": "not found"})
                return
            result = handler(body)
            self._send(200, result if result is not None else {"ok": True})
        except (OSError, ConnectionError) as exc:
            self._send(200, {"ok": False,
                             "error": f"router unreachable: {exc}"})
        except Exception as exc:
            self._send(200, {"ok": False,
                             "error": f"{type(exc).__name__}: {exc}"})


# -- API actions -----------------------------------------------------------

def api_scan(body):
    with STATE_LOCK:
        if STATE["scan"]["running"]:
            return {"ok": False, "error": "a scan is already running"}
    BLINKER.stop()
    threading.Thread(target=scan_worker, daemon=True).start()
    return {"ok": True}


def api_flash(body):
    address = body.get("address")
    steady = bool(body.get("steady"))
    if not address:
        device = current_device()
        if device is None:
            return {"ok": False, "error": "nothing to flash yet - scan first"}
        address = device["address"]
    BLINKER.start(address, steady=steady)
    return {"ok": True, "address": address}


def api_stop_flash(body):
    BLINKER.stop()
    return {"ok": True}


def api_assign(body):
    island_id = body.get("island_id")
    address = body.get("address")
    if island_id is None:
        return {"ok": False, "error": "island_id required"}
    with STATE_LOCK:
        if address is None:
            device = current_device()
            if device is None:
                return {"ok": False, "error": "no current device"}
            address = device["address"]
        STATE["assignments"][address] = int(island_id)
        if address in STATE["skipped"]:
            STATE["skipped"].remove(address)
        advance_to_next_unmapped()
        save_state()
        next_device = STATE["devices"][STATE["cursor"]] if STATE["devices"] else None
    BLINKER.stop()
    if next_device is not None and bool(body.get("auto_flash", True)):
        BLINKER.start(next_device["address"], steady=bool(body.get("steady")))
    return {"ok": True}


def api_unassign(body):
    address = body.get("address")
    with STATE_LOCK:
        if address is None:
            device = current_device()
            if device is None:
                return {"ok": False, "error": "no current device"}
            address = device["address"]
        STATE["assignments"].pop(address, None)
        save_state()
    return {"ok": True}


def api_skip(body):
    with STATE_LOCK:
        device = current_device()
        if device is None:
            return {"ok": False, "error": "no current device"}
        if device["address"] not in STATE["skipped"]:
            STATE["skipped"].append(device["address"])
        advance_to_next_unmapped()
        save_state()
        next_device = STATE["devices"][STATE["cursor"]]
    BLINKER.stop()
    if bool(body.get("auto_flash", True)):
        BLINKER.start(next_device["address"], steady=bool(body.get("steady")))
    return {"ok": True}


def api_cursor(body):
    delta = body.get("delta")
    index = body.get("index")
    with STATE_LOCK:
        if index is not None:
            STATE["cursor"] = int(index)
        elif delta is not None:
            STATE["cursor"] += int(delta)
        clamp_cursor()
        save_state()
        device = STATE["devices"][STATE["cursor"]] if STATE["devices"] else None
    BLINKER.stop()
    if device is not None and bool(body.get("auto_flash", True)):
        BLINKER.start(device["address"], steady=bool(body.get("steady")))
    return {"ok": True}


def api_add_island(body):
    name = (body.get("name") or "").strip()
    with STATE_LOCK:
        next_id = max((i["id"] for i in STATE["islands"]), default=0) + 1
        if not name:
            name = "Island %d" % next_id
        STATE["islands"].append({"id": next_id, "name": name})
        save_state()
    return {"ok": True, "id": next_id}


def api_create_islands(body):
    try:
        count = int(body.get("count", 0))
    except (TypeError, ValueError):
        return {"ok": False, "error": "count must be a number"}
    if count < 1 or count > 500:
        return {"ok": False, "error": "count must be between 1 and 500"}
    with STATE_LOCK:
        next_id = max((i["id"] for i in STATE["islands"]), default=0) + 1
        for offset in range(count):
            island_id = next_id + offset
            STATE["islands"].append(
                {"id": island_id, "name": "Island %d" % island_id})
        save_state()
    return {"ok": True}


def api_rename_island(body):
    island_id = body.get("id")
    name = (body.get("name") or "").strip()
    if island_id is None or not name:
        return {"ok": False, "error": "id and name required"}
    with STATE_LOCK:
        for island in STATE["islands"]:
            if island["id"] == int(island_id):
                island["name"] = name
                break
        save_state()
    return {"ok": True}


def api_delete_island(body):
    island_id = body.get("id")
    if island_id is None:
        return {"ok": False, "error": "id required"}
    island_id = int(island_id)
    with STATE_LOCK:
        STATE["islands"] = [i for i in STATE["islands"] if i["id"] != island_id]
        STATE["assignments"] = {a: i for a, i in STATE["assignments"].items()
                                if int(i) != island_id}
        save_state()
    return {"ok": True}


def api_all_off(body):
    BLINKER.stop()
    errors = 0
    with STATE_LOCK:
        addresses = [d["address"] for d in STATE["devices"]]
    for address in addresses:
        try:
            CLIENT.direct_level_device(address, 0)
        except (OSError, ConnectionError):
            errors += 1
    return {"ok": errors == 0, "count": len(addresses), "errors": errors}


def api_island_on(body):
    """Light every address already mapped to an island - the verification step."""
    island_id = body.get("island_id")
    level = int(body.get("level", 100))
    if island_id is None:
        return {"ok": False, "error": "island_id required"}
    with STATE_LOCK:
        addresses = [a for a, i in STATE["assignments"].items()
                     if int(i) == int(island_id)]
    BLINKER.stop()
    for address in addresses:
        CLIENT.direct_level_device(address, level)
    return {"ok": True, "count": len(addresses)}


def api_raw(body):
    command = (body.get("command") or "").strip()
    if not command:
        return {"ok": False, "error": "command required"}
    reply = CLIENT.send(command, expect_reply=True, timeout=1.5)
    return {"ok": True, "reply": reply or "(no reply)"}


def api_clear_scan_error(body):
    with STATE_LOCK:
        STATE["scan"]["error"] = None
    return {"ok": True}


ROUTES = {
    "/api/scan": api_scan,
    "/api/flash": api_flash,
    "/api/stop_flash": api_stop_flash,
    "/api/assign": api_assign,
    "/api/unassign": api_unassign,
    "/api/skip": api_skip,
    "/api/cursor": api_cursor,
    "/api/island/add": api_add_island,
    "/api/island/create_many": api_create_islands,
    "/api/island/rename": api_rename_island,
    "/api/island/delete": api_delete_island,
    "/api/island/on": api_island_on,
    "/api/all_off": api_all_off,
    "/api/raw": api_raw,
    "/api/clear_scan_error": api_clear_scan_error,
}


# --------------------------------------------------------------------------
# The page. Dark, large touch targets, sized for a 10-11" tablet in landscape.
# --------------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Helvar Mapper</title>
<style>
  :root {
    --bg: #14161a;
    --panel: #1d2026;
    --panel-2: #262a32;
    --line: #333944;
    --text: #e8eaed;
    --muted: #9aa2ae;
    --accent: #ffb020;
    --accent-dim: #6b4a12;
    --ok: #3ddc84;
    --danger: #ff5d55;
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 16px/1.4 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    padding: 0 16px 40px;
    user-select: none;
  }
  header {
    display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
    padding: 14px 0 10px; border-bottom: 1px solid var(--line);
    position: sticky; top: 0; background: var(--bg); z-index: 5;
  }
  h1 { font-size: 17px; margin: 0; font-weight: 600; letter-spacing: .01em; }
  .spacer { flex: 1 1 auto; }
  .pill {
    font-size: 13px; color: var(--muted); background: var(--panel);
    border: 1px solid var(--line); border-radius: 999px; padding: 5px 11px;
    white-space: nowrap;
  }
  .pill b { color: var(--text); font-weight: 600; }
  button {
    font: inherit; color: var(--text); background: var(--panel-2);
    border: 1px solid var(--line); border-radius: 10px;
    padding: 12px 16px; cursor: pointer; min-height: 48px;
  }
  button:active { transform: translateY(1px); }
  button.primary { background: var(--accent); color: #1a1205; border-color: var(--accent); font-weight: 650; }
  button.danger { border-color: #5a2a27; color: #ffb3ae; }
  button.ghost { background: transparent; }
  button:disabled { opacity: .4; cursor: default; }
  nav { display: flex; gap: 8px; margin: 14px 0; }
  nav button { flex: 1 1 0; padding: 11px 8px; min-height: 46px; }
  nav button.on { background: var(--panel-2); border-color: var(--accent); color: var(--accent); }
  section { display: none; }
  section.on { display: block; }

  .card {
    background: var(--panel); border: 1px solid var(--line);
    border-radius: 14px; padding: 16px; margin-bottom: 14px;
  }
  .addr { font-size: 26px; font-weight: 700; letter-spacing: .02em; font-variant-numeric: tabular-nums; }
  .sub { color: var(--muted); font-size: 14px; margin-top: 4px; min-height: 20px; }
  .row { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 14px; }
  .row > button { flex: 1 1 108px; }

  .islands {
    display: grid; gap: 10px;
    grid-template-columns: repeat(auto-fill, minmax(116px, 1fr));
  }
  .island {
    background: var(--panel-2); border: 1px solid var(--line);
    border-radius: 12px; padding: 12px 8px; text-align: center;
    min-height: 72px; display: flex; flex-direction: column;
    align-items: center; justify-content: center; gap: 4px; cursor: pointer;
  }
  .island .nm { font-size: 14px; font-weight: 600; }
  .island .ct { font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; }
  .island.has { border-color: var(--accent-dim); }
  .island.has .ct { color: var(--accent); }

  .bar { height: 8px; background: var(--panel-2); border-radius: 999px; overflow: hidden; margin-top: 10px; }
  .bar > i { display: block; height: 100%; background: var(--accent); width: 0; transition: width .2s; }

  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th, td { text-align: left; padding: 9px 8px; border-bottom: 1px solid var(--line); }
  th { color: var(--muted); font-weight: 500; font-size: 12px; text-transform: uppercase; letter-spacing: .05em; }
  td.mono { font-variant-numeric: tabular-nums; }
  input[type=text], input[type=number] {
    font: inherit; color: var(--text); background: var(--bg);
    border: 1px solid var(--line); border-radius: 10px; padding: 12px;
    min-height: 48px; width: 100%;
  }
  .inline { display: flex; gap: 10px; align-items: center; }
  .inline > input { flex: 1 1 auto; }
  .inline > button { flex: 0 0 auto; }
  .muted { color: var(--muted); font-size: 14px; }
  .toast {
    position: fixed; left: 50%; transform: translateX(-50%); bottom: 22px;
    background: var(--panel-2); border: 1px solid var(--line);
    padding: 12px 18px; border-radius: 10px; font-size: 14px;
    opacity: 0; pointer-events: none; transition: opacity .2s; z-index: 20;
    max-width: 90vw;
  }
  .toast.on { opacity: 1; }
  .toast.err { border-color: var(--danger); color: #ffc9c5; }
  pre { background: var(--bg); border: 1px solid var(--line); border-radius: 10px;
        padding: 12px; overflow-x: auto; font-size: 13px; margin: 10px 0 0; }
  @media (max-width: 560px) { .addr { font-size: 22px; } .row > button { flex: 1 1 100%; } }
</style>
</head>
<body>

<header>
  <h1>Helvar Mapper</h1>
  <span class="pill" id="routerPill">router</span>
  <span class="spacer"></span>
  <span class="pill" id="remainPill">-</span>
  <button class="danger" onclick="allOff()">All off</button>
</header>

<nav>
  <button id="tabWalk" class="on" onclick="tab('Walk')">Walk</button>
  <button id="tabIslands" onclick="tab('Islands')">Islands</button>
  <button id="tabTools" onclick="tab('Tools')">Tools</button>
</nav>

<!-- ------------------------------------------------------------------ -->
<section id="secWalk" class="on">
  <div class="card">
    <div class="addr" id="curAddr">no devices yet</div>
    <div class="sub" id="curSub">Run a scan from the Tools tab.</div>
    <div class="bar"><i id="walkBar"></i></div>
    <div class="row">
      <button onclick="flash(false)">Blink</button>
      <button onclick="flash(true)">Steady on</button>
      <button class="ghost" onclick="stopFlash()">Off</button>
      <button class="ghost" onclick="cursor(-1)">&larr; Back</button>
      <button class="ghost" onclick="skip()">Skip &rarr;</button>
      <button class="ghost" onclick="unassign()">Unassign</button>
    </div>
  </div>

  <div class="card">
    <div class="muted" style="margin-bottom:10px; font-size:13px">
      Tap the island this luminaire belongs to - the next unmapped address
      then starts blinking.
    </div>
    <div class="islands" id="walkIslands"></div>
  </div>
</section>

<!-- ------------------------------------------------------------------ -->
<section id="secIslands">
  <div class="card">
    <div class="inline">
      <input type="number" id="bulkCount" value="39" min="1" max="500">
      <button class="primary" onclick="createMany()">Create islands</button>
    </div>
    <div class="muted" style="margin-top:10px">
      Creates numbered islands in one go. Rename them below, or later.
      Tap an island to light everything already mapped to it.
    </div>
  </div>
  <div class="card">
    <table>
      <thead><tr><th>Island</th><th>Lums</th><th></th></tr></thead>
      <tbody id="islandRows"></tbody>
    </table>
    <div class="row"><button onclick="addIsland()">+ Add one island</button></div>
  </div>
</section>

<!-- ------------------------------------------------------------------ -->
<section id="secTools">
  <div class="card">
    <b>Scan for devices</b>
    <div class="muted" style="margin:8px 0 12px">
      Probes every address on every configured subnet and keeps the ones that
      answer. Safe to re-run; it adds new devices without losing your mapping.
    </div>
    <div class="bar"><i id="scanBar"></i></div>
    <div class="row">
      <button class="primary" id="scanBtn" onclick="scan()">Start scan</button>
    </div>
    <div class="muted" id="scanInfo"></div>
  </div>

  <div class="card">
    <b>Export</b>
    <div class="muted" style="margin:8px 0 12px">
      Writes islands.json next to the script - this is what the scene panel
      will read.
    </div>
    <div class="row"><button onclick="doExport()">Write islands.json</button></div>
    <pre id="exportOut" style="display:none"></pre>
  </div>

  <div class="card">
    <b>Raw HelvarNet command</b>
    <div class="muted" style="margin:8px 0 12px">
      For checking command numbers by hand. Example:
      &gt;V:1,C:14,L:100,F:0,@1.1.1.1#
    </div>
    <div class="inline">
      <input type="text" id="rawCmd" placeholder="&gt;V:1,C:103,@1.1.1.1#"
             autocapitalize="off" autocorrect="off" spellcheck="false">
      <button onclick="sendRaw()">Send</button>
    </div>
    <pre id="rawOut" style="display:none"></pre>
  </div>

  <div class="card">
    <b>Mapped devices</b>
    <table>
      <thead><tr><th>Address</th><th>Island</th><th>Description</th></tr></thead>
      <tbody id="deviceRows"></tbody>
    </table>
  </div>
</section>

<div class="toast" id="toast"></div>

<script>
var S = null;
var steadyMode = false;
var busy = false;

function toast(msg, isError) {
  var el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'toast on' + (isError ? ' err' : '');
  clearTimeout(el._t);
  el._t = setTimeout(function () { el.className = 'toast'; }, 2600);
}

function post(path, body) {
  return fetch(path, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body || {})
  }).then(function (r) { return r.json(); }).then(function (j) {
    if (j && j.ok === false && j.error) toast(j.error, true);
    refresh();
    return j;
  }).catch(function (e) { toast('server unreachable: ' + e, true); });
}

function tab(name) {
  ['Walk', 'Islands', 'Tools'].forEach(function (n) {
    document.getElementById('sec' + n).className = (n === name) ? 'on' : '';
    document.getElementById('tab' + n).className = (n === name) ? 'on' : '';
  });
}

function refresh() {
  return fetch('/api/state').then(function (r) { return r.json(); })
    .then(function (s) { S = s; render(); })
    .catch(function () {});
}

function render() {
  if (!S) return;
  var rp = document.getElementById('routerPill');
  if (S.last_error) {
    rp.innerHTML = 'router <b>unreachable</b>';
    rp.style.borderColor = 'var(--danger)';
    rp.title = S.last_error;
  } else {
    rp.innerHTML = 'router <b>' + S.router_ip + '</b>';
    rp.style.borderColor = '';
    rp.title = '';
  }
  var mapped = Object.keys(S.assignments).length;
  document.getElementById('remainPill').innerHTML =
    '<b>' + mapped + '</b> mapped / <b>' + S.remaining + '</b> left';

  // --- current device ---
  var addrEl = document.getElementById('curAddr');
  var subEl = document.getElementById('curSub');
  if (S.current) {
    addrEl.textContent = S.current.address;
    var bits = [];
    bits.push((S.cursor + 1) + ' of ' + S.devices.length);
    if (S.current.description) bits.push(S.current.description);
    if (S.current.type) bits.push('type ' + S.current.type);
    var assigned = S.assignments[S.current.address];
    if (assigned !== undefined) bits.push('already on ' + islandName(assigned));
    if (S.blinking === S.current.address) bits.push(S.steady ? 'steady on' : 'blinking');
    subEl.textContent = bits.join('  -  ');
  } else {
    addrEl.textContent = 'no devices yet';
    subEl.textContent = 'Run a scan from the Tools tab.';
  }
  var total = S.devices.length || 1;
  document.getElementById('walkBar').style.width =
    Math.round(100 * mapped / total) + '%';

  // --- island buttons ---
  var grid = document.getElementById('walkIslands');
  var sig = S.islands.map(function (i) { return i.id + ':' + i.name; }).join('|');
  if (sig !== grid._sig) {
    grid._sig = sig;
    grid._tiles = {};
    grid.innerHTML = '';
    if (!S.islands.length) {
      grid.innerHTML = '<div class="muted">No islands yet - create them on the ' +
                       'Islands tab.</div>';
    }
    S.islands.forEach(function (isl) {
      var d = document.createElement('div');
      d.className = 'island';
      d.innerHTML = '<div class="nm"></div><div class="ct"></div>';
      d.querySelector('.nm').textContent = isl.name;
      d.onclick = function () { assign(isl.id); };
      grid.appendChild(d);
      grid._tiles[isl.id] = d;
    });
  }
  S.islands.forEach(function (isl) {
    var d = grid._tiles[isl.id];
    if (!d) return;
    var n = S.counts[isl.id] || 0;
    d.className = 'island' + (n ? ' has' : '');
    d.querySelector('.ct').textContent = n + ' lum';
  });

  // --- island table ---
  var rows = document.getElementById('islandRows');
  var rowSig = S.islands.map(function (i) { return i.id; }).join(',');
  if (rowSig !== rows._sig) {
    rows._sig = rowSig;
    rows._rows = {};
    rows.innerHTML = '';
    S.islands.forEach(function (isl) {
      var tr = document.createElement('tr');
      var td1 = document.createElement('td');
      var inp = document.createElement('input');
      inp.type = 'text';
      inp.onchange = function () {
        post('/api/island/rename', {id: isl.id, name: inp.value});
      };
      td1.appendChild(inp);
      var td2 = document.createElement('td');
      td2.className = 'mono';
      var td3 = document.createElement('td');
      var onBtn = document.createElement('button');
      onBtn.textContent = 'Light';
      onBtn.onclick = function () { post('/api/island/on', {island_id: isl.id}); };
      var delBtn = document.createElement('button');
      delBtn.className = 'danger'; delBtn.textContent = 'Delete';
      delBtn.style.marginLeft = '8px';
      delBtn.onclick = function () {
        if (confirm('Delete this island and unassign its luminaires?')) {
          post('/api/island/delete', {id: isl.id});
        }
      };
      td3.appendChild(onBtn); td3.appendChild(delBtn);
      tr.appendChild(td1); tr.appendChild(td2); tr.appendChild(td3);
      rows.appendChild(tr);
      rows._rows[isl.id] = {input: inp, count: td2};
    });
  }
  S.islands.forEach(function (isl) {
    var r = rows._rows[isl.id];
    if (!r) return;
    // never clobber a name the user is part-way through typing
    if (document.activeElement !== r.input) r.input.value = isl.name;
    r.count.textContent = S.counts[isl.id] || 0;
  });

  // --- scan progress ---
  var sc = S.scan;
  var pct = sc.total ? Math.round(100 * sc.done / sc.total) : 0;
  document.getElementById('scanBar').style.width = pct + '%';
  document.getElementById('scanBtn').disabled = !!sc.running;
  document.getElementById('scanBtn').textContent =
    sc.running ? 'Scanning...' : 'Start scan';
  var info = '';
  if (sc.total) info = sc.done + ' / ' + sc.total + ' probed, ' + sc.found + ' found';
  if (sc.error) info += (info ? '  -  ' : '') + 'error: ' + sc.error;
  document.getElementById('scanInfo').textContent = info;

  // --- device table ---
  var drows = document.getElementById('deviceRows');
  var devSig = S.devices.length + '|' + JSON.stringify(S.assignments);
  if (devSig === drows._sig) return;
  drows._sig = devSig;
  drows.innerHTML = '';
  S.devices.forEach(function (d, i) {
    var tr = document.createElement('tr');
    var a = document.createElement('td');
    a.className = 'mono';
    a.textContent = d.address;
    a.style.cursor = 'pointer';
    a.onclick = function () { tab('Walk'); post('/api/cursor', {index: i, steady: steadyMode}); };
    var b = document.createElement('td');
    var asg = S.assignments[d.address];
    b.textContent = asg === undefined ? '-' : islandName(asg);
    var c = document.createElement('td');
    c.textContent = d.description || '';
    tr.appendChild(a); tr.appendChild(b); tr.appendChild(c);
    drows.appendChild(tr);
  });
}

function islandName(id) {
  var found = null;
  S.islands.forEach(function (i) { if (i.id === Number(id)) found = i.name; });
  return found || ('island ' + id);
}

function flash(steady) { steadyMode = steady; post('/api/flash', {steady: steady}); }
function stopFlash() { post('/api/stop_flash', {}); }
function assign(id) { post('/api/assign', {island_id: id, steady: steadyMode}); }
function skip() { post('/api/skip', {steady: steadyMode}); }
function unassign() { post('/api/unassign', {}); }
function cursor(d) { post('/api/cursor', {delta: d, steady: steadyMode}); }
function scan() { post('/api/scan', {}); }
function addIsland() { post('/api/island/add', {}); }
function allOff() { post('/api/all_off', {}).then(function () { toast('all off'); }); }

function createMany() {
  var n = Number(document.getElementById('bulkCount').value);
  post('/api/island/create_many', {count: n});
}

function doExport() {
  fetch('/api/export').then(function (r) { return r.json(); }).then(function (j) {
    var pre = document.getElementById('exportOut');
    pre.style.display = 'block';
    pre.textContent = JSON.stringify(j, null, 2);
    toast('islands.json written');
  });
}

function sendRaw() {
  var cmd = document.getElementById('rawCmd').value;
  post('/api/raw', {command: cmd}).then(function (j) {
    var pre = document.getElementById('rawOut');
    pre.style.display = 'block';
    pre.textContent = (j && j.reply) ? j.reply : (j && j.error) || '(nothing)';
  });
}

document.getElementById('rawCmd').addEventListener('keydown', function (e) {
  if (e.key === 'Enter') sendRaw();
});

refresh();
setInterval(refresh, 1500);
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------

class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    port = CONFIG["http_port"]
    print("Helvar mapper")
    print(f"  router   {CONFIG['router_ip']}:{CONFIG['router_port']}")
    print(f"  subnets  {CONFIG['subnets']} x 1-{CONFIG['max_device_address']}")
    print(f"  config   {CONFIG_PATH}")
    print(f"  state    {STATE_PATH}")
    try:
        host_ip = socket.gethostbyname(socket.gethostname())
    except OSError:
        host_ip = "this-machine"
    print(f"\n  open  http://{host_ip}:{port}/  on the tablet\n")

    server = Server(("0.0.0.0", port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        BLINKER.stop()
        server.server_close()


if __name__ == "__main__":
    main()
