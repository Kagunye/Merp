#!/usr/bin/env python3
"""Mofisi bridge: lets the Mofisi till in the browser drive receipt printers
and cash drawers on the shop's LAN (ESC/POS over TCP, usually port 9100).

A web page cannot open a raw network connection, so the till sends the bytes
here, on the same PC, and this writes them to the printer:

    browser  --POST http://127.0.0.1:9180/send {host, port, data(base64)}-->  bridge
    bridge   --TCP 192.168.x.y:9100-->  printer  --drawer port-->  cash drawer

Safe by default:
  * listens on 127.0.0.1 only (nothing else on the network can use it);
  * only talks to printers on private addresses (10.x, 172.16-31.x, 192.168.x,
    or .local names), never to the internet;
  * only answers pages from the origins you allow (--origin), and can require
    a shared token (--token) sent as the X-Bridge-Token header;
  * works with no internet at all: the till keeps printing and opening the
    drawer while offline.

Run (Python 3.8+, no extra packages):
    python mofisi_bridge.py --origin https://your-mofisi-site.example
Options: --port 9180  --origin URL (repeatable; default any)  --token SECRET
Start with Windows: see start-mofisi-bridge.bat in this folder.
"""
import argparse
import base64
import ipaddress
import json
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.0"
MAX_BYTES = 512 * 1024


def allowed_host(host):
    """A printer on the shop's network: a private IP, or a local name."""
    host = (host or "").strip()
    if not host or len(host) > 253:
        return False
    try:
        ip = ipaddress.ip_address(socket.gethostbyname(host))
    except (OSError, ValueError):
        return False
    if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        return False
    return (ip.is_private and not ip.is_loopback) or (ip.is_loopback and Bridge.allow_local)


class Bridge(BaseHTTPRequestHandler):
    server_version = f"MofisiBridge/{VERSION}"
    origins = []
    token = ""
    allow_local = False

    # ── helpers ──
    def _cors(self):
        origin = self.headers.get("Origin", "")
        if not self.origins or origin in self.origins:
            self.send_header("Access-Control-Allow-Origin", origin or "*")
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Bridge-Token")
        # Chrome's Private Network Access: a public HTTPS page may call 127.0.0.1.
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def _reply(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _origin_ok(self):
        origin = self.headers.get("Origin", "")
        return not self.origins or origin in self.origins

    def log_message(self, fmt, *args):   # quieter console
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ── routes ──
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path.rstrip("/") in ("", "/health"):
            return self._reply(200, {"ok": True, "name": "Mofisi bridge", "version": VERSION})
        self._reply(404, {"ok": False, "error": "Not found"})

    def do_POST(self):
        if self.path.rstrip("/") != "/send":
            return self._reply(404, {"ok": False, "error": "Not found"})
        if not self._origin_ok():
            return self._reply(403, {"ok": False, "error": "This page is not allowed to use the bridge (--origin)."})
        if self.token and self.headers.get("X-Bridge-Token", "") != self.token:
            return self._reply(403, {"ok": False, "error": "Wrong or missing bridge token."})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BYTES * 2:
                return self._reply(413, {"ok": False, "error": "Too much data."})
            data = json.loads(self.rfile.read(length) or b"{}")
            host, port = str(data.get("host", "")), int(data.get("port") or 9100)
            raw = base64.b64decode(data.get("data") or "")
        except (ValueError, TypeError):
            return self._reply(400, {"ok": False, "error": "Bad request."})
        if not allowed_host(host):
            return self._reply(400, {"ok": False, "error": f"{host} is not a printer on the shop network."})
        if not (1 <= port <= 65535) or not raw or len(raw) > MAX_BYTES:
            return self._reply(400, {"ok": False, "error": "Bad port or empty job."})
        try:
            with socket.create_connection((host, port), timeout=5) as sock:
                sock.sendall(raw)
        except OSError as exc:
            return self._reply(502, {"ok": False, "error": f"Could not reach the printer at {host}:{port} ({exc})."})
        self._reply(200, {"ok": True, "sent": len(raw)})


def main(argv=None):
    ap = argparse.ArgumentParser(description="Mofisi bridge for LAN receipt printers and cash drawers")
    ap.add_argument("--port", type=int, default=9180)
    ap.add_argument("--origin", action="append", default=[],
                    help="Mofisi site allowed to use the bridge, e.g. https://erp.example.co.ke (repeatable)")
    ap.add_argument("--token", default="", help="Optional shared secret (X-Bridge-Token)")
    ap.add_argument("--allow-local", action="store_true",
                    help="Testing only: also send to printers on this PC (127.0.0.1)")
    args = ap.parse_args(argv)
    Bridge.origins = [o.rstrip("/") for o in args.origin]
    Bridge.token = args.token
    Bridge.allow_local = args.allow_local
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Bridge)
    print(f"Mofisi bridge {VERSION} listening on http://127.0.0.1:{args.port} "
          f"(origins: {', '.join(Bridge.origins) or 'any'})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
