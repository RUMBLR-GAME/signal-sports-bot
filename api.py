"""
api.py — Dashboard API (3 engines)
GET /api/state — full bot state
GET /health — health check
"""

import logging
import time
from aiohttp import web
from config import CORS_ORIGINS, PAPER_MODE, STARTING_BANKROLL
from positions import PositionManager

logger = logging.getLogger("api")


def _cors(req):
    origin = req.headers.get("Origin", "")
    allowed = origin if any(a.strip() in origin for a in CORS_ORIGINS) else "*"
    return {"Access-Control-Allow-Origin": allowed, "Access-Control-Allow-Methods": "GET, OPTIONS", "Access-Control-Allow-Headers": "Content-Type"}


def create_api(positions: PositionManager, bot_state: dict) -> web.Application:
    app = web.Application()

    async def options(req): return web.Response(headers=_cors(req))

    async def health(req):
        return web.json_response({"status": "ok", "uptime": time.time() - bot_state.get("started_at", time.time())}, headers=_cors(req))

    async def state(req):
        try:
            open_pos = [{
                "id": p.id, "engine": p.engine, "sport": p.sport,
                "market": p.market_question, "team": p.team, "side": p.side,
                "entry_price": p.entry_price, "size": p.size, "cost": p.cost,
                "confidence": p.confidence, "status": p.status,
                "opened_at": p.opened_at, "score_line": p.score_line,
                "true_prob": p.true_prob, "edge": p.edge_at_entry,
            } for p in positions.get_open_positions()]

            history = [{
                "id": t.id, "engine": t.engine, "sport": t.sport,
                "market": t.market_question, "team": t.team, "side": t.side,
                "entry_price": t.entry_price, "size": t.size, "cost": t.cost,
                "confidence": t.confidence, "result": t.result,
                "payout": t.payout, "pnl": t.pnl, "pnl_pct": t.pnl_pct,
                "opened_at": t.opened_at, "closed_at": t.closed_at,
                "score_line": t.score_line, "true_prob": t.true_prob,
                "edge": t.edge_at_entry, "exit_reason": t.exit_reason,
            } for t in reversed(positions.trades)]

            eq_curve = []
            running = STARTING_BANKROLL
            for t in positions.trades:
                running += t.pnl
                eq_curve.append({"time": t.closed_at, "equity": round(running, 2)})

            return web.json_response({
                "timestamp": time.time(),
                "paper_mode": PAPER_MODE,
                "starting_bankroll": STARTING_BANKROLL,
                "equity": round(positions.equity, 2),
                "total_equity": round(positions.total_equity, 2),
                "total_pnl": round(positions.total_pnl, 2),
                "open_cost": round(positions.open_cost, 2),
                "open_positions": open_pos,
                "trade_history": history,
                "equity_curve": eq_curve,
                "harvest_stats": positions.stats("harvest"),
                "edge_stats": positions.stats("edge"),
                "arber_stats": positions.stats("arber"),
                "overall_stats": positions.stats(),
                "last_harvest_scan": bot_state.get("last_harvest_scan"),
                "last_edge_scan": bot_state.get("last_edge_scan"),
                "last_arber_scan": bot_state.get("last_arber_scan"),
                "last_resolve_check": bot_state.get("last_resolve_check"),
                "scan_count": bot_state.get("scan_count", 0),
                "uptime": time.time() - bot_state.get("started_at", time.time()),
                "live_games": bot_state.get("live_games", []),
                "scan_log": bot_state.get("scan_log", []),
                "edges_found": bot_state.get("edges_found", []),
                "markets_scanned": bot_state.get("markets_scanned", 0),
                "blowout_log": bot_state.get("blowout_log", []),
                "poly_diag": getattr(bot_state.get("clob"), "_poly_diag", {}),
            }, headers=_cors(req))
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500, headers=_cors(req))

    for path in ["/api/state", "/health"]:
        app.router.add_route("OPTIONS", path, options)
    app.router.add_route("GET", "/api/state", state)
    app.router.add_route("GET", "/health", health)
    app.router.add_route("GET", "/", health)
    return app
