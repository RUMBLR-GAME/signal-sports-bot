"""
api.py — Dashboard REST API (v18)

GET  /api/state      — full bot state for dashboard
GET  /health         — liveness
POST /api/pause      — manual pause: {"minutes": 60}
POST /api/resume     — clear manual pause
POST /api/reset      — (dangerous) wipe state — requires {"confirm": "RESET"}

CORS-enabled. Adds drawdown, circuit state, per-sport stats, per-engine unrealized.
"""
import logging
import time
from aiohttp import web

from config import CORS_ORIGINS, PAPER_MODE, STARTING_BANKROLL
from positions import PositionManager

logger = logging.getLogger("api")


def _cors_headers(req):
    origin = req.headers.get("Origin", "")
    allowed = origin if any(a.strip() and a.strip() in origin for a in CORS_ORIGINS) else "*"
    return {
        "Access-Control-Allow-Origin": allowed,
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }


def create_api(positions: PositionManager, bot_state: dict) -> web.Application:
    app = web.Application()

    async def options(req):
        return web.Response(headers=_cors_headers(req))

    async def health(req):
        return web.json_response(
            {
                "status": "ok",
                "uptime": time.time() - bot_state.get("started_at", time.time()),
                "paused": bot_state.get("paused_until", 0) > time.time(),
                "ws_sports_connected": bot_state.get("ws_sports_connected", False),
                "ws_market_connected": bot_state.get("ws_market_connected", False),
            },
            headers=_cors_headers(req),
        )

    async def state(req):
        try:
            # Open positions with mark-to-market
            open_pos = []
            for p in positions.get_open_positions():
                mark = p.current_price if p.current_price is not None else p.entry_price
                open_pos.append({
                    "id": p.id, "engine": p.engine, "sport": p.sport,
                    "market": p.market_question,
                    "team": p.team, "bet_outcome": p.bet_outcome,
                    "entry_price": p.entry_price, "fill_price": p.fill_price,
                    "current_price": mark,
                    "size": p.size, "cost": p.cost,
                    "market_value": round(p.size * mark, 2),
                    "unrealized_pnl": round(p.size * mark - p.cost, 2),
                    "confidence": p.confidence,
                    "true_prob": p.true_prob, "edge": p.edge_at_entry,
                    "status": p.status,
                    "opened_at": p.opened_at,
                    "game_start_time": p.game_start_time,
                    "partial_exits": p.partial_exits,
                    "score_line": p.score_line,
                    "token_id": p.token_id,
                })

            history = []
            for t in reversed(positions.trades):
                history.append({
                    "id": t.id, "engine": t.engine, "sport": t.sport,
                    "market": t.market_question,
                    "team": t.team, "bet_outcome": t.bet_outcome,
                    "entry_price": t.entry_price,
                    "exit_price": t.exit_price,
                    "size": t.size, "cost": t.cost,
                    "confidence": t.confidence,
                    "result": t.result, "payout": t.payout,
                    "pnl": t.pnl, "pnl_pct": t.pnl_pct,
                    "opened_at": t.opened_at, "closed_at": t.closed_at,
                    "score_line": t.score_line,
                    "true_prob": t.true_prob, "edge": t.edge_at_entry,
                    "exit_reason": t.exit_reason,
                })

            # Per-sport stats breakdown
            sports_stats = {}
            all_sports = set(t.sport for t in positions.trades) | set(p.sport for p in positions.get_open_positions())
            for sp in all_sports:
                sp_trades = [t for t in positions.trades if t.sport == sp]
                scoring = [t for t in sp_trades if t.result not in ("PARTIAL",)]
                wins = [t for t in scoring if t.result in ("WIN", "EXIT_PROFIT")]
                losses = [t for t in scoring if t.result in ("LOSS", "EXIT_LOSS")]
                sports_stats[sp] = {
                    "total_trades": len(scoring),
                    "wins": len(wins), "losses": len(losses),
                    "win_rate": len(wins) / (len(wins)+len(losses)) if wins or losses else 0,
                    "pnl": round(sum(t.pnl for t in sp_trades), 2),
                    "open": len([p for p in positions.get_open_positions() if p.sport == sp]),
                    "deployed": round(positions.deployed_by_sport(sp), 2),
                }

            return web.json_response(
                {
                    "timestamp": time.time(),
                    "paper_mode": PAPER_MODE,
                    "starting_bankroll": STARTING_BANKROLL,
                    # Equity
                    "cash": round(positions.cash, 2),
                    "equity": round(positions.equity, 2),
                    "total_equity": round(positions.equity, 2),
                    "peak_equity": round(positions.peak_equity, 2),
                    "drawdown_pct": round(positions.drawdown_pct, 4),
                    "total_pnl": round(positions.total_pnl, 2),
                    "unrealized_pnl": round(positions.unrealized_pnl, 2),
                    "open_cost": round(positions.open_cost, 2),
                    # Positions & trades
                    "open_positions": open_pos,
                    "trade_history": history,
                    "equity_curve": positions.equity_curve,
                    # Stats
                    "harvest_stats": positions.stats("harvest"),
                    "edge_stats": positions.stats("edge"),
                    "overall_stats": positions.stats(),
                    "sports_stats": sports_stats,
                    # Engine state
                    "last_harvest_scan": bot_state.get("last_harvest_scan"),
                    "last_edge_scan": bot_state.get("last_edge_scan"),
                    "last_resolve_check": bot_state.get("last_resolve_check"),
                    "last_ws_sports_msg": bot_state.get("last_ws_sports_msg"),
                    "scan_count": bot_state.get("scan_count", 0),
                    "uptime": time.time() - bot_state.get("started_at", time.time()),
                    # Live data
                    "live_games": bot_state.get("live_games", []),
                    "scan_log": bot_state.get("scan_log", []),
                    "edges_found": bot_state.get("edges_found", []),
                    "blowout_log": bot_state.get("blowout_log", []),
                    "sports_with_odds": bot_state.get("sports_with_odds", []),
                    "poly_diag": bot_state.get("poly_diag", {}),
                    "series_verified": bot_state.get("series_verified", {}),
                    "markets_scanned": bot_state.get("markets_scanned", 0),
                    # Flags
                    "ws_sports_connected": bot_state.get("ws_sports_connected", False),
                    "ws_market_connected": bot_state.get("ws_market_connected", False),
                    "odds_api_enabled": bot_state.get("odds_api_enabled", False),
                    "harvest_enabled": bot_state.get("harvest_enabled", True),
                    "edge_enabled": bot_state.get("edge_enabled", True),
                    "lineup_watcher_enabled": bot_state.get("lineup_watcher_enabled", False),
                    # Lineup watcher
                    "lineup_signals": bot_state.get("lineup_signals", []),
                    "lineup_api_budget": bot_state.get("lineup_api_budget", {"used": 0, "limit": 0, "remaining": 0}),
                    # Infra
                    "redis_connected": getattr(positions, "_redis_ok", False),
                    # Safety
                    "circuit": positions.circuit,
                    "paused_until": bot_state.get("paused_until", 0),
                    "paused": bot_state.get("paused_until", 0) > time.time(),
                },
                headers=_cors_headers(req),
            )
        except Exception as e:
            logger.error(f"api/state: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500, headers=_cors_headers(req))

    async def pause(req):
        try:
            body = await req.json()
        except Exception:
            body = {}
        minutes = int(body.get("minutes", 60))
        bot_state["paused_until"] = time.time() + minutes * 60
        logger.warning(f"Manual pause {minutes}m")
        return web.json_response({"ok": True, "paused_minutes": minutes}, headers=_cors_headers(req))

    async def resume(req):
        bot_state["paused_until"] = 0
        logger.warning("Manual resume")
        return web.json_response({"ok": True}, headers=_cors_headers(req))

    async def reset(req):
        try:
            body = await req.json()
        except Exception:
            body = {}
        if body.get("confirm") != "RESET":
            return web.json_response({"error": "requires confirm=RESET"}, status=400, headers=_cors_headers(req))
        await positions._reset()
        return web.json_response({"ok": True, "reset": True}, headers=_cors_headers(req))

    for path in ["/api/state", "/health", "/api/pause", "/api/resume", "/api/reset"]:
        app.router.add_route("OPTIONS", path, options)
    app.router.add_route("GET", "/api/state", state)
    app.router.add_route("GET", "/health", health)
    app.router.add_route("GET", "/", health)
    app.router.add_route("POST", "/api/pause", pause)
    app.router.add_route("POST", "/api/resume", resume)
    app.router.add_route("POST", "/api/reset", reset)
    return app
