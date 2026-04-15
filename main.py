"""
main.py — Signal Harvest Bot Orchestrator
Single-threaded main loop running both engines with API server.
Scan → Execute → Resolve cycle.
"""

import asyncio
import logging
import sys
import time
import uuid

from aiohttp import web

from config import (
    PAPER_MODE, API_PORT, STARTING_BANKROLL,
    HARVEST_INTERVAL, SHARP_INTERVAL, RESOLVE_INTERVAL,
    MAX_UNFILLED_AGE, FILL_CHECK_DELAY_MS,
)
from clob import ClobInterface
from positions import PositionManager, Position
from harvest import scan_harvest
from sharp import scan_sharp
from odds import reset_daily_calls
from api import create_api

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)-10s] %(levelname)-5s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("main")


async def execute_signal(signal, clob: ClobInterface, positions: PositionManager):
    """Execute a trade signal. Places maker limit order and records position."""
    if signal.clob_price <= 0:
        return
    shares = round(signal.kelly_size / signal.clob_price, 2)
    if shares < 1:
        return

    result = clob.place_order(
        token_id=signal.token_id,
        price=signal.clob_price,
        size=shares,
        side="BUY",
    )

    if result is None:
        logger.warning(f"Order failed for {signal.team}")
        return

    order_id = result.get("orderID", f"unknown-{int(time.time())}")

    position = Position(
        id=str(uuid.uuid4())[:12],
        engine=signal.engine,
        sport=signal.sport,
        market_question=signal.market_question,
        condition_id=signal.condition_id,
        team=signal.team,
        side=signal.side,
        token_id=signal.token_id,
        entry_price=signal.clob_price,
        size=shares,
        cost=round(signal.clob_price * shares, 2),
        confidence=signal.confidence,
        order_id=order_id,
        status="filled" if PAPER_MODE else "open",
        score_line=getattr(signal, "score_line", ""),
        pinnacle_prob=getattr(signal, "pinnacle_prob", 0.0),
        edge=signal.edge,
    )

    if PAPER_MODE:
        position.fill_price = signal.clob_price
        position.filled_at = time.time()

    await positions.open_position(position)
    logger.info(f"✅ Executed: {signal.engine} | {signal.team} {signal.side}@{signal.clob_price} x{shares}")


async def check_fills(clob: ClobInterface, positions: PositionManager):
    """Poll open orders to detect fills (notifications can be lost)."""
    if PAPER_MODE:
        return

    open_orders = clob.get_open_orders()
    if not open_orders:
        return

    filled_ids = set()
    for order in open_orders:
        status = order.get("status", "").lower()
        if status in ("filled", "matched"):
            filled_ids.add(order.get("id", ""))

    for pos in positions.get_open_positions():
        if pos.status == "open" and pos.order_id in filled_ids:
            await asyncio.sleep(FILL_CHECK_DELAY_MS / 1000)
            await positions.mark_filled(pos.id)


async def cancel_stale_orders(clob: ClobInterface, positions: PositionManager):
    """Cancel unfilled orders older than MAX_UNFILLED_AGE."""
    stale = positions.get_stale_orders(MAX_UNFILLED_AGE)
    for pos in stale:
        logger.info(f"Cancelling stale order {pos.order_id} ({pos.team})")
        clob.cancel_order(pos.order_id)
        await positions.cancel_position(pos.id)


async def check_resolutions(clob: ClobInterface, positions: PositionManager):
    """Check if any filled positions have resolved."""
    filled = [p for p in positions.get_open_positions() if p.status == "filled"]
    for pos in filled:
        result = await clob.check_resolution(pos.condition_id)
        if result is None:
            continue
        if not result.get("resolved", False):
            continue
        winner = result.get("winner", "UNKNOWN")
        await positions.resolve_position(pos.id, winner)


async def bot_loop(clob: ClobInterface, positions: PositionManager, bot_state: dict):
    """Main bot loop with independent timers for each engine."""
    last_harvest = 0.0
    last_sharp = 0.0
    last_resolve = 0.0
    last_daily_reset = time.time()
    harvest_scans = 0
    sharp_scans = 0

    mode_label = "📝 PAPER" if PAPER_MODE else "💰 LIVE"
    logger.info(f"{'='*60}")
    logger.info(f"  Signal Harvest Bot — {mode_label} MODE")
    logger.info(f"  Starting bankroll: ${STARTING_BANKROLL:.2f}")
    logger.info(f"  Equity: ${positions.equity:.2f}")
    logger.info(f"  Open positions: {len(positions.get_open_positions())}")
    logger.info(f"  Resolved trades: {len(positions.trades)}")
    logger.info(f"{'='*60}")

    while True:
        try:
            now = time.time()

            # Daily quota reset (midnight-ish)
            if now - last_daily_reset > 86400:
                reset_daily_calls()
                last_daily_reset = now
                logger.info("Daily Odds API call counter reset")

            # ── HARVEST SCAN ─────────────────────────────────────
            if now - last_harvest >= HARVEST_INTERVAL:
                last_harvest = now
                harvest_scans += 1
                bot_state["last_harvest_scan"] = now
                bot_state["scan_count"] = harvest_scans + sharp_scans

                try:
                    harvest_signals, live_games = await scan_harvest(clob, positions)

                    # Update dashboard live games
                    bot_state["live_games"] = live_games

                    for signal in harvest_signals:
                        await execute_signal(signal, clob, positions)

                except Exception as e:
                    logger.error(f"Harvest scan error: {e}", exc_info=True)

            # ── SHARP EDGE SCAN ──────────────────────────────────
            if now - last_sharp >= SHARP_INTERVAL:
                last_sharp = now
                sharp_scans += 1
                bot_state["last_sharp_scan"] = now
                bot_state["scan_count"] = harvest_scans + sharp_scans

                try:
                    sharp_signals = await scan_sharp(clob, positions)

                    for signal in sharp_signals:
                        await execute_signal(signal, clob, positions)

                except Exception as e:
                    logger.error(f"Sharp scan error: {e}", exc_info=True)

            # ── FILL CHECK + STALE CLEANUP ───────────────────────
            try:
                await check_fills(clob, positions)
                await cancel_stale_orders(clob, positions)
            except Exception as e:
                logger.error(f"Fill check error: {e}", exc_info=True)

            # ── RESOLUTION CHECK ─────────────────────────────────
            if now - last_resolve >= RESOLVE_INTERVAL:
                last_resolve = now
                bot_state["last_resolve_check"] = now

                try:
                    await check_resolutions(clob, positions)
                except Exception as e:
                    logger.error(f"Resolution check error: {e}", exc_info=True)

            await asyncio.sleep(5)

        except asyncio.CancelledError:
            logger.info("Bot loop cancelled")
            break
        except Exception as e:
            logger.error(f"Main loop error: {e}", exc_info=True)
            await asyncio.sleep(10)


async def main():
    bot_state = {
        "started_at": time.time(),
        "scan_count": 0,
        "live_games": [],
        "sharp_comparisons": [],
    }

    clob = ClobInterface()
    if not clob.initialize():
        logger.error("CLOB init failed — running degraded")

    positions = PositionManager()
    await positions.initialize()

    app = create_api(positions, bot_state)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", API_PORT)
    await site.start()
    logger.info(f"API server on port {API_PORT}")

    try:
        await bot_loop(clob, positions, bot_state)
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt")
    finally:
        await clob.close()
        await runner.cleanup()
        logger.info("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
