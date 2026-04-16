"""
main.py — Signal Harvest v4.1 Orchestrator
Three engines: Harvest (blowouts), Edge Finder (convergence), Poly Arber (arbitrage)
Edge exit loop runs every 30s — pre-game exit is time-critical.
"""

import asyncio, logging, sys, time, uuid
from aiohttp import web
from config import (
    PAPER_MODE, API_PORT, STARTING_BANKROLL,
    HARVEST_INTERVAL, EDGE_SCAN_INTERVAL, EDGE_EXIT_INTERVAL,
    ARBER_INTERVAL, RESOLVE_INTERVAL, MAX_UNFILLED_AGE, FILL_CHECK_DELAY_MS,
    EDGE_MIN_EDGE,
)
from clob import ClobInterface
from positions import PositionManager, Position
from harvest import scan_harvest
from edge import scan_edge, check_edge_exits
from arber import scan_arber
from api import create_api

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)-8s] %(levelname)-5s %(message)s", datefmt="%H:%M:%S", stream=sys.stdout)
logger = logging.getLogger("main")


async def execute_signal(signal, clob, positions):
    price = getattr(signal, "clob_price", 0)
    bet = getattr(signal, "bet_size", 0)
    if price <= 0 or bet <= 0:
        return
    shares = round(bet / price, 2)
    if shares < 1:
        return
    result = clob.place_order(token_id=signal.token_id, price=price, size=shares, side="BUY")
    if not result:
        return

    pos = Position(
        id=str(uuid.uuid4())[:12], engine=signal.engine,
        sport=getattr(signal, "sport", ""),
        market_question=getattr(signal, "market_question", ""),
        condition_id=getattr(signal, "condition_id", ""),
        team=getattr(signal, "team", ""), side=getattr(signal, "side", ""),
        token_id=signal.token_id, entry_price=price, size=shares,
        cost=round(price * shares, 2), confidence=getattr(signal, "confidence", 0),
        order_id=result.get("orderID", ""),
        status="filled" if PAPER_MODE else "open",
        true_prob=getattr(signal, "true_prob", 0),
        edge_at_entry=getattr(signal, "edge", 0),
        game_start_time=getattr(signal, "commence_time", ""),
        score_line=getattr(signal, "score_line", ""),
        espn_id=getattr(signal, "espn_id", ""),
    )
    if PAPER_MODE:
        pos.fill_price = price
        pos.filled_at = time.time()
    await positions.open_position(pos)


async def execute_arber(signal, clob, positions):
    if not signal.buys or signal.bet_size <= 0:
        return
    sets = int(signal.bet_size / signal.total_cost)
    if sets < 1:
        return
    for buy in signal.buys:
        result = clob.place_order(token_id=buy["token_id"], price=buy["price"], size=sets, side="BUY")
        if not result:
            continue
        pos = Position(
            id=str(uuid.uuid4())[:12], engine="arber", sport=signal.sport,
            market_question=signal.market_question, condition_id=signal.condition_id,
            team=buy["side"], side=buy["side"], token_id=buy["token_id"],
            entry_price=buy["price"], size=sets, cost=round(buy["price"] * sets, 2),
            confidence=0.99, order_id=result.get("orderID", ""),
            status="filled" if PAPER_MODE else "open",
        )
        if PAPER_MODE:
            pos.fill_price = buy["price"]
            pos.filled_at = time.time()
        await positions.open_position(pos)


async def check_fills(clob, positions):
    if PAPER_MODE:
        return
    for order in clob.get_open_orders():
        if order.get("status", "").lower() in ("filled", "matched"):
            for pos in positions.get_open_positions():
                if pos.status == "open" and pos.order_id == order.get("id"):
                    await asyncio.sleep(FILL_CHECK_DELAY_MS / 1000)
                    await positions.mark_filled(pos.id)


async def cancel_stale(clob, positions):
    for pos in positions.get_stale_orders(MAX_UNFILLED_AGE):
        clob.cancel_order(pos.order_id)
        await positions.cancel_position(pos.id)


async def check_resolutions(clob, positions):
    for pos in [p for p in positions.get_open_positions() if p.status == "filled"]:
        # Don't resolve Edge positions — they exit before game
        if pos.engine == "edge":
            continue
        result = await clob.check_resolution(pos.condition_id)
        if result and result.get("resolved"):
            await positions.resolve_position(pos.id, result.get("winner", "UNKNOWN"))


async def bot_loop(clob, positions, bot_state):
    last = {"harvest": 0, "edge_scan": 0, "edge_exit": 0, "arber": 0, "resolve": 0}
    scans = 0

    mode = "📝 PAPER" if PAPER_MODE else "💰 LIVE"
    logger.info(f"{'='*55}")
    logger.info(f"  Signal Harvest v4.1 — {mode}")
    logger.info(f"  Harvest (blowouts) + Edge Finder (convergence) + Poly Arber")
    logger.info(f"  Equity: ${positions.equity:.2f} | Open: {len(positions.get_open_positions())}")
    logger.info(f"{'='*55}")

    while True:
        try:
            now = time.time()
            log = bot_state.get("log_event", lambda *a,**k: None)

            # HARVEST — every 30s
            if now - last["harvest"] >= HARVEST_INTERVAL:
                last["harvest"] = now
                scans += 1
                bot_state.update(last_harvest_scan=now, scan_count=scans)
                try:
                    signals, live_games, blowout_log = await scan_harvest(clob, positions)

                    # Enrich live games with Polymarket odds (home_price, away_price, home_prob, away_prob)
                    from clob import parse_market_tokens
                    from teams import find_team_in_outcomes
                    sport_cache = {}
                    for g in live_games:
                        sport = g.get("sport")
                        if not sport:
                            continue
                        if sport not in sport_cache:
                            try:
                                sport_cache[sport] = await clob.fetch_polymarket_events(sport)
                            except Exception:
                                sport_cache[sport] = []

                        # Find matching Polymarket market
                        for ev in sport_cache[sport]:
                            parsed = parse_market_tokens(ev)
                            if not parsed:
                                continue
                            outcomes = parsed["outcomes"]
                            hi = find_team_in_outcomes(g["home_team"], g["home_abbrev"], outcomes)
                            ai = find_team_in_outcomes(g["away_team"], g["away_abbrev"], outcomes)
                            if hi < 0 or ai < 0 or hi == ai:
                                continue
                            # Got a match — fetch real-time prices
                            try:
                                hp = clob.get_price(parsed["token_ids"][hi], "BUY")
                                ap = clob.get_price(parsed["token_ids"][ai], "BUY")
                                if hp is not None:
                                    g["home_poly"] = round(hp, 3)
                                if ap is not None:
                                    g["away_poly"] = round(ap, 3)
                                g["market"] = parsed["question"][:60]
                            except Exception:
                                pass
                            # Also attach blowout true_prob if available
                            bl = next((b for b in blowout_log if b.get("leader") in (g["home_abbrev"], g["away_abbrev"])), None)
                            if bl:
                                leader_ab = bl["leader"]
                                if leader_ab == g["home_abbrev"]:
                                    g["home_true_prob"] = round(bl["confidence"], 3)
                                    g["away_true_prob"] = round(1 - bl["confidence"], 3)
                                elif leader_ab == g["away_abbrev"]:
                                    g["away_true_prob"] = round(bl["confidence"], 3)
                                    g["home_true_prob"] = round(1 - bl["confidence"], 3)
                            break

                    bot_state["live_games"] = live_games
                    bot_state["blowout_log"] = blowout_log
                    if live_games:
                        log(f"Monitoring {len(live_games)} live games", engine="harvest")
                    if blowout_log:
                        for b in blowout_log:
                            if b["status"] == "signal":
                                log(f"🎯 {b['leader']} +{b['lead']} — TRADING @{b.get('price',0):.3f}", level="signal", engine="harvest")
                            elif b["status"] == "skip":
                                log(f"⚠️ {b['leader']} +{b['lead']} — {b['reason']}", engine="harvest")
                    for s in signals:
                        await execute_signal(s, clob, positions)
                        log(f"🎯 BUY {s.game.leader} YES@{s.clob_price:.2f} edge={s.edge:.1%}", level="trade", engine="harvest")
                except Exception as e:
                    logger.error(f"Harvest: {e}", exc_info=True)

            # EDGE SCAN — every 2 min (find new convergence trades)
            if now - last["edge_scan"] >= EDGE_SCAN_INTERVAL:
                last["edge_scan"] = now
                bot_state["last_edge_scan"] = now
                try:
                    edge_signals, all_edges = await scan_edge(clob, positions)
                    # Store ALL edges for dashboard scanner (including sub-threshold)
                    bot_state["edges_found"] = all_edges
                    if edge_signals:
                        log(f"⚡ {len(edge_signals)} tradeable edges (of {len(all_edges)} detected)", level="signal", engine="edge")
                    elif all_edges:
                        log(f"Scanned — {len(all_edges)} edges found, none above {int(EDGE_MIN_EDGE*100)}% threshold", engine="edge")
                    else:
                        log(f"Scanned odds — no edges detected", engine="edge")
                    for s in edge_signals:
                        await execute_signal(s, clob, positions)
                        log(f"⚡ BUY {s.team} @{s.clob_price:.2f} (true {s.true_prob:.2f}, edge {s.edge:.1%})", level="trade", engine="edge")
                except Exception as e:
                    logger.error(f"Edge scan: {e}", exc_info=True)

            # EDGE EXIT — every 30s (pre-game exit is time-critical!)
            if now - last["edge_exit"] >= EDGE_EXIT_INTERVAL:
                last["edge_exit"] = now
                try:
                    await check_edge_exits(clob, positions)
                except Exception as e:
                    logger.error(f"Edge exit: {e}", exc_info=True)

            # ARBER — every 3 min
            if now - last["arber"] >= ARBER_INTERVAL:
                last["arber"] = now
                bot_state["last_arber_scan"] = now
                try:
                    arber_signals = await scan_arber(clob, positions)
                    bot_state["markets_scanned"] = bot_state.get("markets_scanned", 0) + 1
                    if arber_signals:
                        log(f"🔄 {len(arber_signals)} arb opportunities", level="signal", engine="arber")
                    for s in arber_signals:
                        await execute_arber(s, clob, positions)
                        log(f"🔄 ARB {s.market_question[:40]}… profit {s.profit_pct:.1%}", level="trade", engine="arber")
                except Exception as e:
                    logger.error(f"Arber: {e}", exc_info=True)

            # FILLS + STALE
            try:
                await check_fills(clob, positions)
                await cancel_stale(clob, positions)
            except Exception as e:
                logger.error(f"Fill/stale: {e}", exc_info=True)

            # RESOLUTION — every 2 min (Harvest + Arber only, not Edge)
            if now - last["resolve"] >= RESOLVE_INTERVAL:
                last["resolve"] = now
                bot_state["last_resolve_check"] = now
                try:
                    await check_resolutions(clob, positions)
                except Exception as e:
                    logger.error(f"Resolution: {e}", exc_info=True)

            await asyncio.sleep(5)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Loop: {e}", exc_info=True)
            await asyncio.sleep(10)


async def main():
    bot_state = {
        "started_at": time.time(), "scan_count": 0, "live_games": [],
        "scan_log": [],        # rolling activity feed for dashboard
        "edges_found": [],     # recent edges (traded or not) for scanner view
        "markets_scanned": 0,  # total markets checked
    }

    def log_event(msg, level="info", engine=""):
        """Add to rolling scan log (keeps last 50 entries)."""
        bot_state["scan_log"].append({
            "t": time.time(), "msg": msg, "level": level, "engine": engine,
        })
        if len(bot_state["scan_log"]) > 50:
            bot_state["scan_log"] = bot_state["scan_log"][-50:]

    bot_state["log_event"] = log_event
    clob = ClobInterface()
    if not clob.initialize():
        logger.error("CLOB init failed — degraded mode")
    positions = PositionManager()
    await positions.initialize()
    app = create_api(positions, bot_state)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", API_PORT).start()
    logger.info(f"API on :{API_PORT}")
    try:
        await bot_loop(clob, positions, bot_state)
    finally:
        await clob.close()
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
