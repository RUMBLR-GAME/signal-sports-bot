"""
api.py — Dashboard API Server
Serves bot state to Vercel dashboard.
Endpoints: GET /api/state, GET /health
"""

import json
import logging
import time

from aiohttp import web

from config import CORS_ORIGINS, PAPER_MODE, STARTING_BANKROLL
from positions import PositionManager
from odds import get_remaining_quota

logger = logging.getLogger("api")


def _cors_headers(request) -> dict:
    origin = request.headers.get("Origin", "")
    if any(allowed.strip() in origin for allowed in CORS_ORIGINS):
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        }
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }


def create_api(positions: PositionManager, bot_state: dict) -> web.Application:
    app = web.Application()

    async def handle_options(request):
        return web.Response(headers=_cors_headers(request))

    async def health(request):
        return web.json_response(
            {"status": "ok", "uptime": time.time() - bot_state.get("started_at", time.time())},
            headers=_cors_headers(request),
        )

    async def get_state(request):
        try:
            open_positions = []
            for p in positions.get_open_positions():
                open_positions.append({
                    "id": p.id, "engine": p.engine, "sport": p.sport,
                    "market": p.market_question, "team": p.team, "side": p.side,
                    "entry_price": p.entry_price, "size": p.size, "cost": p.cost,
                    "confidence": p.confidence, "status": p.status,
                    "opened_at": p.opened_at, "score_line": p.score_line,
                    "pinnacle_prob": p.pinnacle_prob, "edge": p.edge,
                })

            trade_history = []
            for t in reversed(positions.trades):
                trade_history.append({
                    "id": t.id, "engine": t.engine, "sport": t.sport,
                    "market": t.market_question, "team": t.team, "side": t.side,
                    "entry_price": t.entry_price, "size": t.size, "cost": t.cost,
                    "confidence": t.confidence, "result": t.result,
                    "payout": t.payout, "pnl": t.pnl, "pnl_pct": t.pnl_pct,
                    "opened_at": t.opened_at, "closed_at": t.closed_at,
                    "score_line": t.score_line, "pinnacle_prob": t.pinnacle_prob,
                    "edge": t.edge,
                })

            equity_curve = []
            running = STARTING_BANKROLL
            for t in positions.trades:
                running += t.pnl
                equity_curve.append({
                    "time": t.closed_at,
                    "equity": round(running, 2),
                    "trade_id": t.id,
                })

            state = {
                "timestamp": time.time(),
                "paper_mode": PAPER_MODE,
                "starting_bankroll": STARTING_BANKROLL,
                "equity": round(positions.equity, 2),
                "total_equity": round(positions.total_equity, 2),
                "total_pnl": round(positions.total_pnl, 2),
                "open_cost": round(positions.open_cost, 2),
                "open_positions": open_positions,
                "trade_history": trade_history,
                "equity_curve": equity_curve,
                "harvest_stats": positions.stats(engine="harvest"),
                "sharp_stats": positions.stats(engine="sharp"),
                "overall_stats": positions.stats(),
                "odds_api_quota": get_remaining_quota(),
                "last_harvest_scan": bot_state.get("last_harvest_scan"),
                "last_sharp_scan": bot_state.get("last_sharp_scan"),
                "last_resolve_check": bot_state.get("last_resolve_check"),
                "scan_count": bot_state.get("scan_count", 0),
                "uptime": time.time() - bot_state.get("started_at", time.time()),
                "live_games": bot_state.get("live_games", []),
                "sharp_comparisons": bot_state.get("sharp_comparisons", []),
            }

            return web.json_response(state, headers=_cors_headers(request))

        except Exception as e:
            logger.error(f"API error: {e}")
            return web.json_response({"error": str(e)}, status=500, headers=_cors_headers(request))

    app.router.add_route("OPTIONS", "/api/state", handle_options)
    app.router.add_route("GET", "/api/state", get_state)
    app.router.add_route("OPTIONS", "/health", handle_options)
    app.router.add_route("GET", "/health", health)
    app.router.add_route("GET", "/", health)

    return app
