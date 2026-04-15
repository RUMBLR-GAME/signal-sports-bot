"""
api.py — Dashboard API Server

Serves /api/state for the Vercel dashboard.
Serves /api/health and /api/clob-status for monitoring.
"""

import json
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone

import config
from clob import health_check

_state = {}
_lock = threading.Lock()
_log: list[dict] = []


def update_state(**kw):
    with _lock:
        _state.update(kw)


def add_log(msg: str):
    with _lock:
        entry = {"t": datetime.now(timezone.utc).isoformat(), "m": msg}
        _log.insert(0, entry)
        if len(_log) > 200:
            del _log[200:]


def get_state() -> dict:
    with _lock:
        return {**_state, "log": _log[:100]}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/api/state", "/"):
            self._json(200, get_state())
        elif self.path == "/api/clob-status":
            self._json(200, health_check())
        elif self.path == "/health":
            self._json(200, {"status": "ok"})
        else:
            self._json(404, {"error": "not found"})

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET")
        self.send_header("Access-Control-Allow-Headers", "*")

    def log_message(self, *a):
        pass


def start():
    server = HTTPServer(("0.0.0.0", config.API_PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"  [API] Port {config.API_PORT}")
