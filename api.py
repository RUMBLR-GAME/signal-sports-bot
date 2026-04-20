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
from clob import ClobInterface

logger = logging.getLogger("api")


def _ml_to_prob(ml):
    """Convert American moneyline odds to implied probability (no de-vig)."""
    if ml is None or ml == 0:
        return None
    try:
        ml = int(ml)
    except (TypeError, ValueError):
        return None
    if ml > 0:
        return round(100.0 / (ml + 100.0), 4)
    else:
        return round(abs(ml) / (abs(ml) + 100.0), 4)


def _cors_headers(req):
    origin = req.headers.get("Origin", "")
    allowed = origin if any(a.strip() and a.strip() in origin for a in CORS_ORIGINS) else "*"
    return {
        "Access-Control-Allow-Origin": allowed,
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }


def create_api(positions: PositionManager, bot_state: dict, clob: ClobInterface = None) -> web.Application:
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
                # Build markers — if you see this, new exit code is deployed
                "build": "v21-fast-exits-2h-fallback",
                "build_features": [
                    "extreme_price_exit_0.96",
                    "extreme_loss_exit_0.04",
                    "age_guard_3min",
                    "take_profit_fallback_5c",
                    "resolution_fallback_2h",
                ],
            },
            headers=_cors_headers(req),
        )

    async def state(req):
        try:
            import time as _time
            now_ts = _time.time()
            # Open positions with mark-to-market
            open_pos = []
            for p in positions.get_open_positions():
                mark = p.current_price if p.current_price is not None else p.entry_price
                entry = p.fill_price or p.entry_price
                age_min = (now_ts - p.opened_at) / 60.0
                max_edge = max(0.0, (p.true_prob or 0) - entry)
                realized = mark - entry
                # Compute WHY this position is still open (for debugging)
                why_open = []
                if mark < 0.96 and mark > 0.04:
                    why_open.append(f"price_mid({mark:.3f})")
                if age_min < 3:
                    why_open.append(f"age_too_new({age_min:.1f}m)")
                if max_edge <= 0:
                    why_open.append("no_max_edge")
                elif realized / max_edge < 0.35:
                    why_open.append(f"tp_at({realized/max_edge:.0%})")
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
                    "age_min": round(age_min, 1),
                    "last_mark_at": p.last_mark_at,
                    "game_start_time": p.game_start_time,
                    "partial_exits": p.partial_exits,
                    "score_line": p.score_line,
                    "token_id": p.token_id,
                    "provider": p.provider,
                    "moneyline": p.moneyline,
                    "book_prob": _ml_to_prob(p.moneyline),
                    "why_held": ", ".join(why_open) if why_open else "awaiting next exit cycle",
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
                    "last_edge_exit_run": bot_state.get("last_edge_exit_run", 0),
                    "edge_exit_runs": bot_state.get("edge_exit_runs", 0),
                    "last_edge_exit_ok": bot_state.get("last_edge_exit_ok", 0),
                    "last_edge_exit_err": bot_state.get("last_edge_exit_err"),
                    "last_resolve_check": bot_state.get("last_resolve_check"),
                    "last_ws_sports_msg": bot_state.get("last_ws_sports_msg"),
                    "scan_count": bot_state.get("scan_count", 0),
                    "uptime": time.time() - bot_state.get("started_at", time.time()),
                    # Live data
                    "live_games": bot_state.get("live_games", []),
                    "scan_log": bot_state.get("scan_log", []),
                    "edges_found": bot_state.get("edges_found", []),
                    "edge_scan_diag": bot_state.get("edge_scan_diag", {}),
                    "odds_source_counts": bot_state.get("odds_source_counts", {}),
                    "oddsapi_league_diag": bot_state.get("oddsapi_league_diag", {}),
                    "scan_history_summary": [
                        {
                            "id": s.get("id"),
                            "engine": s.get("engine"),
                            "ts": s.get("ts"),
                            "duration_ms": s.get("duration_ms"),
                            "total_findings": s.get("total_findings", 0),
                            "signals": s.get("signals", 0),
                        }
                        for s in bot_state.get("scan_history", [])[-50:]
                    ],
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

    async def close_position(req):
        """POST /api/close/{position_id} — force close a single open position at current market price."""
        pid = req.match_info.get("position_id", "")
        p = positions.positions.get(pid)
        if not p:
            return web.json_response({"error": f"position {pid} not found"}, status=404, headers=_cors_headers(req))
        # Fetch current SELL (bid) side price — that's what we'd actually get
        current = await clob.get_price_http(p.token_id, "SELL")
        if current is None:
            # Fallback to last marked price if we have it
            current = getattr(p, "current_price", None) or p.entry_price
        trade = await positions.force_close(pid, current, reason="api_close")
        if not trade:
            return web.json_response({"error": "close failed"}, status=500, headers=_cors_headers(req))
        return web.json_response({
            "ok": True, "position_id": pid, "exit_price": current,
            "pnl": trade.pnl, "pnl_pct": trade.pnl_pct,
        }, headers=_cors_headers(req))

    async def close_all(req):
        """POST /api/close-all — force close ALL open positions. Requires confirm=CLOSE_ALL."""
        try:
            body = await req.json()
        except Exception:
            body = {}
        if body.get("confirm") != "CLOSE_ALL":
            return web.json_response(
                {"error": "requires confirm=CLOSE_ALL"}, status=400, headers=_cors_headers(req)
            )
        closed = []
        # Snapshot IDs first — force_close mutates self.positions
        ids = list(positions.positions.keys())
        for pid in ids:
            p = positions.positions.get(pid)
            if not p:
                continue
            current = await clob.get_price_http(p.token_id, "SELL")
            if current is None:
                current = getattr(p, "current_price", None) or p.entry_price
            trade = await positions.force_close(pid, current, reason="close_all")
            if trade:
                closed.append({
                    "position_id": pid, "team": p.team, "exit_price": current,
                    "pnl": trade.pnl,
                })
        return web.json_response({
            "ok": True, "closed": len(closed),
            "total_pnl": sum(c["pnl"] for c in closed),
            "trades": closed,
        }, headers=_cors_headers(req))

    async def scans(req):
        """GET /api/scans?limit=100&engine=edge — full scan history with findings.
        Query params:
          limit: max entries to return (default 100, cap 500)
          engine: filter by 'edge' or 'harvest'
        """
        try:
            limit = min(500, int(req.query.get("limit", 100)))
        except ValueError:
            limit = 100
        eng = req.query.get("engine", "").lower()
        history = bot_state.get("scan_history", [])
        if eng in ("edge", "harvest"):
            history = [s for s in history if s.get("engine") == eng]
        # Return newest first, up to limit
        history = list(reversed(history))[:limit]
        return web.json_response({
            "scans": history,
            "total_retained": len(bot_state.get("scan_history", [])),
        }, headers=_cors_headers(req))

    async def debug_exits(req):
        """GET /api/debug/exits — dry-run the exit logic on every edge position and
        return what WOULD happen. Useful for debugging 'positions not exiting'."""
        from config import EDGE_TAKE_PROFIT_PCT, EDGE_STOP_LOSS, EDGE_STALE_HOURS, EDGE_PRE_GAME_EXIT_MIN
        now_ts = time.time()
        results = []
        for p in positions.get_filled_by_engine("edge"):
            # Fetch current price same way the exit loop does
            current = None
            if clob:
                try:
                    current = await clob.get_midpoint_http(p.token_id)
                except Exception:
                    current = None
            if current is None:
                current = getattr(p, "current_price", None) or p.entry_price
            entry = p.fill_price or p.entry_price
            age_min = (now_ts - p.opened_at) / 60.0
            max_edge = max(0.0, (p.true_prob or 0) - entry)
            realized = current - entry
            # Run the same decision tree
            decision = "HOLD"
            reason = ""
            mins_to_game = None
            if p.game_start_time:
                try:
                    from datetime import datetime
                    start_ts = datetime.fromisoformat(p.game_start_time.replace("Z", "+00:00")).timestamp()
                    mins_to_game = (start_ts - now_ts) / 60.0
                except Exception:
                    pass
            if mins_to_game is not None and mins_to_game <= EDGE_PRE_GAME_EXIT_MIN:
                decision = "EXIT"; reason = f"pre_game T-{mins_to_game:.0f}m"
            elif current >= 0.96 and current > entry:
                decision = "EXIT"; reason = f"extreme_price @{current:.3f}"
            elif current <= 0.04:
                decision = "EXIT"; reason = f"extreme_loss @{current:.3f}"
            elif max_edge > 0 and age_min >= 3 and realized / max_edge >= EDGE_TAKE_PROFIT_PCT:
                decision = "EXIT"; reason = f"take_profit {realized/max_edge:.0%} of edge"
            elif max_edge <= 0 and realized >= 0.05 and age_min >= 3:
                decision = "EXIT"; reason = f"take_profit_fallback +{realized*100:.1f}c"
            elif current < entry - EDGE_STOP_LOSS:
                decision = "EXIT"; reason = f"stop_loss @{current:.3f}"
            elif age_min > EDGE_STALE_HOURS * 60:
                decision = "EXIT"; reason = f"stale {age_min:.0f}m"
            else:
                if age_min < 3: reason = f"age_guard ({age_min:.1f}m < 3m)"
                elif max_edge > 0: reason = f"tp_progress {realized/max_edge:.0%} < 35%"
                else: reason = "waiting for edge move"
            results.append({
                "id": p.id, "team": p.team, "sport": p.sport,
                "entry": round(entry, 3), "current": round(current, 3),
                "true_prob": round(p.true_prob or 0, 3),
                "max_edge": round(max_edge, 3), "realized": round(realized, 3),
                "age_min": round(age_min, 1), "mins_to_game": round(mins_to_game, 1) if mins_to_game is not None else None,
                "decision": decision, "reason": reason,
            })
        return web.json_response({
            "count": len(results),
            "would_exit": sum(1 for r in results if r["decision"] == "EXIT"),
            "last_edge_exit_run": bot_state.get("last_edge_exit_run"),
            "last_edge_exit_ok": bot_state.get("last_edge_exit_ok"),
            "last_edge_exit_err": bot_state.get("last_edge_exit_err"),
            "edge_exit_runs": bot_state.get("edge_exit_runs", 0),
            "positions": results,
        }, headers=_cors_headers(req))

    for path in ["/api/state", "/health", "/api/pause", "/api/resume", "/api/reset", "/api/close-all", "/api/scans", "/api/debug/exits"]:
        app.router.add_route("OPTIONS", path, options)
    app.router.add_route("GET", "/api/state", state)
    app.router.add_route("GET", "/api/scans", scans)
    app.router.add_route("GET", "/api/debug/exits", debug_exits)
    app.router.add_route("GET", "/health", health)
    app.router.add_route("GET", "/", health)
    app.router.add_route("POST", "/api/pause", pause)
    app.router.add_route("POST", "/api/resume", resume)
    app.router.add_route("POST", "/api/reset", reset)
    app.router.add_route("POST", "/api/close/{position_id}", close_position)
    app.router.add_route("POST", "/api/close-all", close_all)
    return app
