"""
main.py — Signal Harvest v18 Orchestrator

Major changes from v17:
  • Single aiohttp session shared across all fetchers (no more churn)
  • Polymarket Sports WebSocket is primary live data source; ESPN fallback
  • Polymarket Market WebSocket for live token prices (replaces per-scan REST)
  • Correct bet_outcome tracking for proper resolution
  • Partial exit support
  • Verified series IDs on startup (cross-check with /sports)
  • Cleaner scan log with engine tagging
  • Safe pause / resume via API
"""
import asyncio
import logging
import sys
import time
import uuid

import aiohttp
from aiohttp import web

from config import (
    PAPER_MODE, API_PORT, STARTING_BANKROLL,
    HARVEST_INTERVAL, EDGE_SCAN_INTERVAL, EDGE_EXIT_INTERVAL,
    RESOLVE_INTERVAL, PARTIAL_CHECK_INTERVAL, EQUITY_CURVE_INTERVAL,
    MAX_UNFILLED_AGE, FILL_CHECK_DELAY_MS,
    EDGE_MIN_EDGE, HARVEST_ENABLED, EDGE_ENABLED,
    ODDS_API_ENABLED, SCAN_LOG_MAX,
    HARVEST_PARTIAL_EXIT_PRICE, HARVEST_PARTIAL_EXIT_FRAC,
)
from clob import ClobInterface, parse_market_tokens
from positions import PositionManager, Position
from harvest import scan_harvest
from edge import scan_edge, check_edge_exits
from api import create_api
from polymarket_ws import SportsWS, MarketWS
from lineup_watcher import LineupWatcher
import espn
import odds_api

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)-10s] %(levelname)-5s %(message)s",
    datefmt="%H:%M:%S", stream=sys.stdout,
)
logger = logging.getLogger("main")


def _log_event(bot_state, msg, level="info", engine=""):
    bot_state["scan_log"].append({
        "t": time.time(), "msg": msg, "level": level, "engine": engine,
    })
    if len(bot_state["scan_log"]) > SCAN_LOG_MAX:
        bot_state["scan_log"] = bot_state["scan_log"][-SCAN_LOG_MAX:]


# ─── Signal execution ──────────────────────────────────────────────────
async def execute_signal(signal, clob: ClobInterface, positions: PositionManager):
    price = getattr(signal, "clob_price", 0)
    bet = getattr(signal, "bet_size", 0)
    if price <= 0 or bet <= 0:
        return
    shares = round(bet / price, 2)
    if shares < 1:
        return

    result = await clob.place_order(signal.token_id, price, shares, "BUY")
    if not result:
        return

    pos = Position(
        id=str(uuid.uuid4())[:12],
        engine=signal.engine, sport=getattr(signal, "sport", ""),
        market_question=getattr(signal, "market_question", ""),
        condition_id=getattr(signal, "condition_id", ""),
        team=getattr(signal, "team", ""),
        bet_outcome=getattr(signal, "bet_outcome", ""),
        bet_is_yes_side=False,  # All sports markets are named-outcome
        outcome_idx=getattr(signal, "outcome_idx", 0),
        token_id=signal.token_id,
        entry_price=price, size=shares,
        cost=round(price * shares, 2),
        confidence=getattr(signal, "confidence", 0),
        order_id=result.get("orderID", ""),
        status="filled" if PAPER_MODE else "open",
        true_prob=getattr(signal, "true_prob", 0),
        edge_at_entry=getattr(signal, "edge", 0),
        game_start_time=getattr(signal, "commence_time", ""),
        score_line=getattr(signal, "score_line", ""),
        espn_id=getattr(signal, "espn_id", ""),
        provider=getattr(signal, "provider", ""),
        moneyline=getattr(signal, "moneyline", 0),
    )
    if PAPER_MODE:
        pos.fill_price = price
        pos.filled_at = time.time()

    await positions.open_position(pos)


async def check_fills(clob, positions):
    if PAPER_MODE:
        return
    for order in await clob.get_open_orders():
        if order.get("status", "").lower() in ("filled", "matched"):
            for pos in positions.get_open_positions():
                if pos.status == "open" and pos.order_id == order.get("id"):
                    await asyncio.sleep(FILL_CHECK_DELAY_MS / 1000)
                    await positions.mark_filled(pos.id)


async def cancel_stale(clob, positions):
    for pos in positions.get_stale_orders(MAX_UNFILLED_AGE):
        await clob.cancel_order(pos.order_id)
        await positions.cancel_position(pos.id)


# ─── Resolution ────────────────────────────────────────────────────────
async def check_resolutions(clob, positions, bot_state):
    for pos in [p for p in positions.get_open_positions() if p.status == "filled"]:
        # Edge positions should exit before the game; skip them here.
        if pos.engine == "edge":
            continue
        result = await clob.check_resolution(pos.condition_id)
        if result and result.get("resolved"):
            winner = result.get("winner", "UNKNOWN")
            yes_price = result.get("yes_price")

            # For named-outcome (2-way) sports markets, translate YES/NO to outcome.
            # Polymarket returns "YES" when outcome_0 won. If our bet was on outcome_0,
            # we won when YES. Otherwise we won when NO.
            if winner == "YES" and pos.outcome_idx == 0:
                effective_winner = pos.bet_outcome
            elif winner == "NO" and pos.outcome_idx == 1:
                effective_winner = pos.bet_outcome
            elif winner in ("YES", "NO"):
                effective_winner = "OTHER"
            else:
                effective_winner = winner
            await positions.resolve_position(pos.id, effective_winner, yes_price=yes_price)
            _log_event(bot_state, f"Resolved {pos.team}: {winner}", engine="resolve")


# ─── Harvest partial exits ────────────────────────────────────────────
async def check_harvest_partials(clob: ClobInterface, positions: PositionManager, bot_state: dict):
    for pos in positions.get_filled_by_engine("harvest"):
        if pos.partial_exits > 0:
            continue
        # Try authenticated SELL first, fall back to HTTP midpoint for paper mode
        current = await clob.get_price(pos.token_id, "SELL")
        if current is None:
            current = await clob.get_midpoint_http(pos.token_id)
        if current is None:
            continue
        positions.mark_current_price(pos.id, current)
        if current >= HARVEST_PARTIAL_EXIT_PRICE:
            # Place sell order in live mode; paper mode just simulates
            if clob.is_authenticated():
                await clob.place_order(
                    pos.token_id, current, pos.size * HARVEST_PARTIAL_EXIT_FRAC, "SELL"
                )
            trade = await positions.partial_close(
                pos.id, current, HARVEST_PARTIAL_EXIT_FRAC,
                f"partial @ {current:.3f}",
            )
            if trade:
                _log_event(
                    bot_state,
                    f"PARTIAL {pos.team} {HARVEST_PARTIAL_EXIT_FRAC*100:.0f}% @{current:.3f}: +${trade.pnl:.2f}",
                    level="trade", engine="harvest",
                )


# ─── Live game price enrichment (throttled) ───────────────────────────
async def enrich_live_games(clob: ClobInterface, bot_state: dict, market_ws: MarketWS):
    """
    Attach Polymarket prices to live games for the dashboard.
    Uses MarketWS cache first (no REST call); falls back to REST rarely.
    Uses shared poly_cache via bot_state["_get_poly_events"].
    """
    live_games = bot_state.get("live_games", [])
    if not live_games:
        return
    get_poly = bot_state.get("_get_poly_events")
    if get_poly is None:
        return

    from teams import match_game_to_market
    tokens_to_subscribe = []

    for g in live_games:
        sport = g.get("sport")
        if not sport:
            continue
        # Clear stale enrichment before re-matching. If parser now rejects
        # a market we previously matched (e.g. after a derivative-title fix),
        # we don't want to keep showing the old bad data.
        g.pop("market", None)
        g.pop("condition_id", None)
        g.pop("home_poly", None)
        g.pop("away_poly", None)
        g.pop("home_token_id", None)
        g.pop("away_token_id", None)
        events = await get_poly(sport)
        for ev in events:
            parsed = parse_market_tokens(ev)
            if not parsed:
                continue
            hi, ai = match_game_to_market(
                g.get("home_team", ""), g.get("home_abbrev", ""),
                g.get("away_team", ""), g.get("away_abbrev", ""),
                parsed["question"], parsed["outcomes"],
            )
            if hi < 0 or ai < 0:
                continue

            home_tok = parsed["token_ids"][hi]
            away_tok = parsed["token_ids"][ai]
            tokens_to_subscribe.extend([home_tok, away_tok])

            # Price fetching: both sides MUST come from the same source
            # so the pair is consistent (sums ≤1 with spread). Otherwise
            # mixing WS-midpoint with REST-ask produces nonsense like 85¢+2¢=87¢.
            home_price = None
            away_price = None
            debug_tag = f"{g.get('away_team','?')[:10]}@{g.get('home_team','?')[:10]}"

            # Strategy 1: both from WS midpoint (preferred — real-time, consistent)
            h_mid = market_ws.midpoint(home_tok)
            a_mid = market_ws.midpoint(away_tok)
            # Reject suspect WS midpoints (empty orderbook returns 0.5)
            h_valid = h_mid is not None and not (0.499 < h_mid < 0.501)
            a_valid = a_mid is not None and not (0.499 < a_mid < 0.501)
            if h_valid and a_valid:
                home_price = h_mid
                away_price = a_mid
                logger.debug(f"enrich {debug_tag}: WS mid home={h_mid:.3f} away={a_mid:.3f}")

            # Strategy 2: both from REST midpoint endpoint
            if home_price is None or away_price is None:
                try:
                    h_rest = await clob.get_midpoint_http(home_tok)
                    a_rest = await clob.get_midpoint_http(away_tok)
                    if h_rest is not None and a_rest is not None:
                        home_price = h_rest
                        away_price = a_rest
                        logger.debug(f"enrich {debug_tag}: REST mid home={h_rest:.3f} away={a_rest:.3f}")
                except Exception as e:
                    logger.debug(f"enrich REST mid err {debug_tag}: {e}")

            # Strategy 3: fall back to Gamma outcomePrices (stale but consistent pair)
            price_is_stale = False
            if home_price is None or away_price is None:
                try:
                    home_price = float(parsed["prices"][hi])
                    away_price = float(parsed["prices"][ai])
                    price_is_stale = True
                    logger.debug(f"enrich {debug_tag}: Gamma stale home={home_price:.3f} away={away_price:.3f}")
                except Exception:
                    home_price = None
                    away_price = None

            if home_price is not None:
                g["home_poly"] = round(home_price, 3)
            if away_price is not None:
                g["away_poly"] = round(away_price, 3)
            g["poly_price_stale"] = price_is_stale
            g["market"] = parsed["question"][:80]
            g["condition_id"] = parsed["condition_id"]
            g["home_token_id"] = home_tok
            g["away_token_id"] = away_tok
            break

    if tokens_to_subscribe:
        await market_ws.subscribe(tokens_to_subscribe)


# ─── Main loop ────────────────────────────────────────────────────────
async def bot_loop(clob, positions, bot_state, sports_ws: SportsWS, market_ws: MarketWS, lineup_watcher=None):
    last = {
        "harvest": 0.0, "edge_scan": 0.0, "edge_exit": 0.0,
        "resolve": 0.0, "partials": 0.0, "equity": 0.0,
    }
    scans = 0
    shared_session: aiohttp.ClientSession = bot_state["session"]

    logger.info("=" * 55)
    logger.info(f"  Signal Harvest v18 — {'PAPER' if PAPER_MODE else 'LIVE'}")
    logger.info(f"  Bankroll: ${STARTING_BANKROLL:.2f}")
    logger.info(f"  Harvest={HARVEST_ENABLED}  Edge={EDGE_ENABLED}  OddsAPI={ODDS_API_ENABLED}")
    logger.info("=" * 55)

    # ESPN fetch cache — shared across harvest and edge scanners within a TTL
    espn_cache = {"ts": 0, "data": ([], [], [])}
    ESPN_CACHE_TTL = 25  # seconds — shorter than HARVEST_INTERVAL so at least one fresh per cycle

    async def _get_espn_data():
        nonlocal espn_cache
        if time.time() - espn_cache["ts"] > ESPN_CACHE_TTL:
            try:
                data = await espn.fetch_all(shared_session)
                espn_cache = {"ts": time.time(), "data": data}
            except Exception as e:
                logger.warning(f"espn cache refresh failed: {e}")
        return espn_cache["data"]

    # Polymarket events cache per sport, shared across harvest/edge/enrich
    poly_cache: dict = {}  # sport → (ts, events)
    POLY_CACHE_TTL = 60

    async def _get_poly_events(sport: str):
        nonlocal poly_cache
        now_ = time.time()
        if sport in poly_cache and now_ - poly_cache[sport][0] < POLY_CACHE_TTL:
            return poly_cache[sport][1]
        events = await clob.fetch_polymarket_events(sport)
        poly_cache[sport] = (now_, events)
        return events

    bot_state["_get_poly_events"] = _get_poly_events

    while True:
        try:
            now = time.time()
            paused = bot_state.get("paused_until", 0) > now
            circuit_ok, circuit_reason = positions.circuit_check()

            # HARVEST scan
            if HARVEST_ENABLED and now - last["harvest"] >= HARVEST_INTERVAL:
                last["harvest"] = now
                scans += 1
                bot_state["scan_count"] = scans
                bot_state["last_harvest_scan"] = now
                try:
                    blowouts, live, _ = await _get_espn_data()
                    bot_state["live_games"] = live
                    bot_state["ws_sports_connected"] = sports_ws.is_connected()
                    bot_state["ws_market_connected"] = market_ws.is_connected()

                    if paused or not circuit_ok:
                        _log_event(
                            bot_state,
                            f"Paused — {'manual' if paused else circuit_reason}",
                            engine="harvest", level="warning",
                        )
                    else:
                        signals, diag = await scan_harvest(clob, positions, blowouts)
                        bot_state["blowout_log"] = diag
                        for s in signals:
                            await execute_signal(s, clob, positions)
                            _log_event(
                                bot_state,
                                f"BUY {s.team} @{s.clob_price:.3f} edge={s.edge:.1%} ${s.bet_size:.2f}",
                                level="trade", engine="harvest",
                            )

                    # Always enrich live games (useful during pause too)
                    await enrich_live_games(clob, bot_state, market_ws)
                    bot_state["poly_diag"] = clob.poly_diag
                    _log_event(
                        bot_state,
                        f"Monitoring {len(live)} live games, {len(blowouts)} blowouts detected",
                        engine="harvest",
                    )
                except Exception as e:
                    logger.error(f"harvest: {e}", exc_info=True)
                    _log_event(bot_state, f"harvest err: {e}", level="error", engine="harvest")

            # EDGE scan (every 2 min)
            if EDGE_ENABLED and now - last["edge_scan"] >= EDGE_SCAN_INTERVAL:
                last["edge_scan"] = now
                bot_state["last_edge_scan"] = now
                try:
                    # Reuse cached ESPN data
                    _, _, espn_odds = await _get_espn_data()
                    oa_odds = await odds_api.fetch_all_soccer(shared_session)
                    all_odds = list(espn_odds) + list(oa_odds)

                    sports_with = sorted({o.sport for o in all_odds})
                    bot_state["sports_with_odds"] = sports_with
                    bot_state["odds_source_counts"] = {
                        "espn_odds": len(espn_odds),
                        "oddsapi_odds": len(oa_odds),
                        "total": len(all_odds),
                        "espn_sports": sorted({o.sport for o in espn_odds}),
                        "oddsapi_sports": sorted({o.sport for o in oa_odds}),
                    }
                    bot_state["oddsapi_league_diag"] = odds_api.get_league_diag()

                    if paused or not circuit_ok:
                        _log_event(bot_state, "Paused", engine="edge", level="warning")
                    else:
                        signals, all_edges, edge_diag = await scan_edge(clob, positions, all_odds, lineup_watcher=lineup_watcher)
                        bot_state["edges_found"] = all_edges
                        bot_state["edge_scan_diag"] = edge_diag
                        if signals:
                            _log_event(
                                bot_state,
                                f"{len(signals)} tradeable (of {len(all_edges)} scanned)",
                                level="signal", engine="edge",
                            )
                        else:
                            _log_event(
                                bot_state,
                                f"Scanned {len(sports_with)} sports — {len(all_edges)} edges, none ≥ {int(EDGE_MIN_EDGE*100)}%",
                                engine="edge",
                            )
                        for s in signals:
                            await execute_signal(s, clob, positions)
                            _log_event(
                                bot_state,
                                f"BUY {s.team} @{s.clob_price:.3f} edge={s.edge:.1%} [{s.provider}] ${s.bet_size:.2f}",
                                level="trade", engine="edge",
                            )
                except Exception as e:
                    logger.error(f"edge: {e}", exc_info=True)

            # EDGE exit (every 30s)
            if EDGE_ENABLED and now - last["edge_exit"] >= EDGE_EXIT_INTERVAL:
                last["edge_exit"] = now
                bot_state["last_edge_exit_run"] = now
                bot_state["edge_exit_runs"] = bot_state.get("edge_exit_runs", 0) + 1
                try:
                    await check_edge_exits(clob, positions)
                    bot_state["last_edge_exit_ok"] = now
                except Exception as e:
                    bot_state["last_edge_exit_err"] = str(e)[:200]
                    logger.error(f"edge exit: {e}", exc_info=True)

            # HARVEST partials (every 60s)
            if HARVEST_ENABLED and now - last["partials"] >= PARTIAL_CHECK_INTERVAL:
                last["partials"] = now
                try:
                    await check_harvest_partials(clob, positions, bot_state)
                except Exception as e:
                    logger.error(f"partials: {e}", exc_info=True)

            # RESOLVE (every 2 min)
            if now - last["resolve"] >= RESOLVE_INTERVAL:
                last["resolve"] = now
                bot_state["last_resolve_check"] = now
                try:
                    await check_resolutions(clob, positions, bot_state)
                except Exception as e:
                    logger.error(f"resolve: {e}", exc_info=True)

            # Equity curve recording
            if now - last["equity"] >= EQUITY_CURVE_INTERVAL:
                last["equity"] = now
                positions.record_equity_point()
                positions.update_peak()

            # Fills + stale cancellation
            try:
                await check_fills(clob, positions)
                await cancel_stale(clob, positions)
            except Exception as e:
                logger.error(f"fills/stale: {e}", exc_info=True)

            # Update WS flags in state
            bot_state["ws_sports_connected"] = sports_ws.is_connected()
            bot_state["ws_market_connected"] = market_ws.is_connected()
            _sec = sports_ws.seconds_since_last_message()
            bot_state["last_ws_sports_msg"] = (time.time() - _sec) if _sec < 1e8 else 0

            # Publish lineup watcher state (if active)
            if lineup_watcher is not None and lineup_watcher.is_enabled():
                bot_state["lineup_signals"] = lineup_watcher.active_signals()
                bot_state["lineup_api_budget"] = lineup_watcher.api_budget()
                bot_state["lineup_watcher_enabled"] = True
            else:
                bot_state["lineup_signals"] = []
                bot_state["lineup_api_budget"] = {"used": 0, "limit": 0, "remaining": 0}
                bot_state["lineup_watcher_enabled"] = False

            await asyncio.sleep(5)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"loop: {e}", exc_info=True)
            await asyncio.sleep(10)


# ─── Entrypoint ────────────────────────────────────────────────────────
async def main():
    bot_state = {
        "started_at": time.time(),
        "scan_count": 0,
        "scan_log": [],
        "live_games": [],
        "edges_found": [],
        "blowout_log": [],
        "markets_scanned": 0,
        "paused_until": 0,
        "harvest_enabled": HARVEST_ENABLED,
        "edge_enabled": EDGE_ENABLED,
        "odds_api_enabled": ODDS_API_ENABLED,
        "ws_sports_connected": False,
        "ws_market_connected": False,
    }

    # Shared session for all REST fetchers (espn, odds_api, clob Gamma)
    session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=15),
        connector=aiohttp.TCPConnector(limit=100, limit_per_host=20),
    )
    bot_state["session"] = session

    # CLOB
    clob = ClobInterface()
    clob.initialize()

    # Verify series IDs against live /sports
    try:
        verified = await clob.verify_series_ids()
        bot_state["series_verified"] = verified
        bad = [s for s, ok in verified.items() if not ok]
        if bad:
            logger.warning(f"Series ID MISMATCH on: {bad}")
    except Exception as e:
        logger.warning(f"series verify failed: {e}")

    # Positions
    positions = PositionManager()
    await positions.initialize()

    # Polymarket WebSockets
    sports_ws = SportsWS()
    market_ws = MarketWS()

    def on_sports_update(g):
        # Merge WS game into live_games (keyed by normalized team pair)
        from teams import normalize as _nz
        key = (_nz(g.get("home_team","")), _nz(g.get("away_team","")))
        found = False
        for existing in bot_state["live_games"]:
            if (_nz(existing.get("home_team","")), _nz(existing.get("away_team",""))) == key:
                existing.update({k: v for k, v in g.items() if v is not None})
                found = True
                break
        if not found:
            bot_state["live_games"].append(g)

    sports_ws.on_update(on_sports_update)
    await sports_ws.start(session)
    await market_ws.start(session)

    # Lineup watcher (flag-gated; inert if no API key)
    lineup_watcher = LineupWatcher()
    await lineup_watcher.start(session)
    bot_state["lineup_watcher_enabled"] = lineup_watcher.is_enabled()

    # HTTP API
    app = create_api(positions, bot_state, clob)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", API_PORT).start()
    logger.info(f"API on :{API_PORT}")

    try:
        await bot_loop(clob, positions, bot_state, sports_ws, market_ws, lineup_watcher)
    finally:
        await sports_ws.stop()
        await market_ws.stop()
        await lineup_watcher.stop()
        await clob.close()
        await session.close()
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("shutdown")
