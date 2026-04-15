"""
api_server.py — Dashboard API for dual-engine bot
"""

import json
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone

_state = {
    "bankroll": 1000, "starting": 1000, "pnl": 0, "equity": 1000,
    "trades": 0, "wins": 0, "win_rate": 0, "open_count": 0,
    "exposure": 0, "harvest_exposure": 0, "synth_exposure": 0,
    "drawdown": 0, "scan_count": 0, "last_scan": None, "scanning": False,
    "harvest_trades": 0, "synth_trades": 0,
    # Harvest data
    "verified_games": [], "harvest_targets": [],
    # Synth data
    "synth_signals": [], "synth_status": {},
    # Shared
    "open_positions": [], "trade_history": [], "log": [],
    "engines": {"harvest": False, "synth": False},
}
_lock = threading.Lock()

def update(**kw):
    with _lock: _state.update(kw)
def set_games(g):
    with _lock: _state["verified_games"] = g[:50]
def set_harvest_targets(t):
    with _lock: _state["harvest_targets"] = t[:50]
def set_synth_signals(s):
    with _lock: _state["synth_signals"] = s[:50]
def add_trade(t):
    with _lock: _state["trade_history"] = [t] + _state["trade_history"][:299]
def add_log(msg):
    with _lock: _state["log"] = [{"t": datetime.now(timezone.utc).isoformat(), "m": msg}] + _state["log"][:149]

# ── CLOB status cache (checked lazily, not every request) ──
_clob_status = {"status": "unknown", "checked": 0}
_clob_lock = threading.Lock()

def _check_clob_status() -> dict:
    """Check CLOB connectivity. Cached for 30s."""
    with _clob_lock:
        if _clob_status["checked"] > 0 and (datetime.now(timezone.utc).timestamp() - _clob_status["checked"]) < 30:
            return _clob_status
    try:
        from polymarket_prices import get_real_price
        # Try fetching a real price — use current 5-min BTC window
        import time as _time
        now = int(_time.time())
        window_ts = now - (now % 300)
        slug = f"btc-updown-5m-{window_ts}"
        result = get_real_price(slug, "up")
        status = {
            "status": "ok" if result else "no_market",
            "checked": datetime.now(timezone.utc).timestamp(),
            "sample": result,
        }
    except Exception as e:
        status = {
            "status": "error",
            "checked": datetime.now(timezone.utc).timestamp(),
            "error": str(e)[:100],
        }
    with _clob_lock:
        _clob_status.update(status)
    return status


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/api/state", "/"):
            with _lock: body = json.dumps(_state).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/clob-status":
            status = _check_clob_status()
            # Strip the sample's token_id for security (it's long and not needed by dashboard)
            safe = {
                "status": status.get("status", "unknown"),
                "checked": status.get("checked", 0),
            }
            if status.get("sample"):
                safe["buy_price"] = status["sample"].get("buy_price")
                safe["spread"] = status["sample"].get("spread")
            if status.get("error"):
                safe["error"] = status["error"]
            self._j(200, safe)
        elif self.path == "/health":
            self._j(200, {"status": "ok"})
        else:
            self._j(404, {"error": "not found"})
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()
    def _j(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass

def start_api():
    port = int(os.environ.get("PORT", 3001))
    server = HTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"  [API] Port {port}")
