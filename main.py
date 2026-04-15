"""
main.py — Signal Harvest Bot v3

Single-engine sports blowout harvester.
Scan → Execute → Track → Resolve → Repeat.

Paper mode: simulates execution, tracks virtual bankroll.
Live mode: places maker limit orders on Polymarket CLOB.
"""

import time
import sys
import os
from datetime import datetime, timezone
from dataclasses import asdict

import config
from scanner import scan, HarvestSignal
from positions import PositionManager
from clob import (
    is_authenticated, place_limit_buy, cancel_order,
    check_order_filled, get_balance, health_check,
)
from api import start as start_api, update_state, add_log

# ── State ──
positions = PositionManager()
bankroll = config.STARTING_BANKROLL  # Paper bankroll (live uses CLOB balance)
scan_count = 0


def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"  [{ts}] {msg}")
    add_log(msg)


def push_dashboard():
    """Push current state to dashboard API."""
    stats = positions.get_stats(bankroll)
    open_pos = positions.get_open()
    update_state(
        **stats,
        open_positions=[asdict(p) for p in open_pos],
        scan_count=scan_count,
        last_scan=datetime.now(timezone.utc).isoformat(),
    )


# ══════════════════════════════════════════════════
# MAIN ENGINE LOOP
# ══════════════════════════════════════════════════

def engine_loop():
    global bankroll, scan_count

    # ── Restore bankroll from persisted state ──
    # bankroll = starting - sum(open costs) + sum(resolved payouts)
    open_cost = sum(p.cost_basis for p in positions.get_open())
    bankroll = config.STARTING_BANKROLL - open_cost + positions.total_pnl
    if positions.total_trades > 0:
        log(f"Restored bankroll: ${bankroll:.2f} ({positions.total_trades} trades, ${positions.total_pnl:+.2f} P&L)")

    while True:
        try:
            scan_count += 1

            # ── Phase 1: Resolve ──
            resolved = positions.check_resolutions()
            for pos in resolved:
                if config.PAPER_MODE:
                    bankroll += pos.shares * pos.exit_price
                icon = "✓" if pos.pnl > 0 else "✗"
                log(f"{icon} RESOLVED {pos.outcome} → ${pos.pnl:+.2f} ({pos.status})")

                # Add resolved trade to dashboard history
                existing = get_existing_trades()
                # Update if already exists, otherwise prepend
                updated = False
                for i, t in enumerate(existing):
                    if t.get("id") == pos.signal_id:
                        existing[i]["status"] = pos.status
                        existing[i]["pnl"] = pos.pnl
                        existing[i]["exitPrice"] = pos.exit_price
                        updated = True
                        break
                if not updated:
                    existing.insert(0, {
                        "id": pos.signal_id, "engine": "harvest", "sport": pos.sport,
                        "event": pos.event, "outcome": pos.outcome,
                        "entryPrice": pos.entry_price, "shares": pos.shares,
                        "cost": pos.cost_basis, "confidence": pos.confidence,
                        "level": pos.level, "scoreLine": pos.detail,
                        "priceSource": pos.price_source, "spread": pos.spread_at_entry,
                        "ev": pos.ev_at_entry, "status": pos.status,
                        "pnl": pos.pnl, "exitPrice": pos.exit_price,
                        "timestamp": pos.resolved_time or pos.entry_time,
                    })
                update_state(trade_history=existing[:300])
                push_dashboard()

            # ── Phase 2: Check fills on pending orders ──
            if not config.PAPER_MODE:
                for pos in positions.positions:
                    if pos.status == "open" and pos.order_id:
                        fill = check_order_filled(pos.order_id)
                        if fill and fill.get("filled"):
                            pos.status = "filled"
                            log(f"FILL {pos.outcome} → {pos.shares}× @ ${pos.entry_price:.4f}")
                            positions._save()

                # Cancel stale unfilled orders
                now = time.time()
                for pos in positions.positions:
                    if pos.status == "open" and pos.order_id and pos.entry_time:
                        age = now - datetime.fromisoformat(pos.entry_time).timestamp()
                        if age > config.ORDER_TIMEOUT:
                            if cancel_order(pos.order_id):
                                positions.cancel(pos)
                                if config.PAPER_MODE:
                                    bankroll += pos.cost_basis
                                log(f"CANCELLED stale order: {pos.outcome} (age {age:.0f}s)")

            # ── Phase 3: Scan for new opportunities ──
            equity = bankroll if config.PAPER_MODE else get_balance().get("balance", bankroll)
            exposure = positions.get_exposure()

            result = scan(equity, exposure)
            # scan() returns (signals, games) tuple
            if isinstance(result, tuple):
                signals, games = result
            else:
                signals, games = result, []

            # Push games to dashboard
            update_state(
                verified_games=[{
                    "sport": g.sport, "home": g.home_abbrev, "away": g.away_abbrev,
                    "homeScore": g.home_score, "awayScore": g.away_score,
                    "leader": g.leader_team, "lead": g.lead,
                    "period": g.period, "clock": g.clock,
                    "elapsed": g.elapsed_pct, "confidence": g.confidence,
                    "level": g.level, "scoreLine": g.score_line,
                } for g in (games if isinstance(games, list) else [])],
                harvest_targets=[{
                    "id": s.id, "sport": s.sport, "event": s.event_title,
                    "outcome": s.outcome, "price": s.price, "shares": s.shares,
                    "cost": s.cost, "return": s.implied_return,
                    "confidence": s.confidence, "level": s.level,
                    "scoreLine": s.score_line, "leader": s.leader_team,
                    "priceSource": s.price_source, "spread": s.spread,
                    "ev": s.ev_per_share,
                } for s in signals],
            )

            if signals:
                log(f"{len(signals)} harvest signals found")

            # ── Phase 4: Execute ──
            for sig in signals[:8]:  # Max 8 trades per cycle
                # Duplicate check
                if positions.has_position(sig.condition_id):
                    continue

                # ── LIVE EXECUTION ──
                if not config.PAPER_MODE and is_authenticated():
                    result = place_limit_buy(
                        token_id=sig.token_id,
                        price=sig.price,
                        size=sig.shares,
                    )
                    if result.success:
                        pos = positions.open(sig, order_id=result.order_id)
                        src = "LIVE" if sig.price_source == "clob" else "GAMMA"
                        log(
                            f"ORDER {sig.shares}× {sig.outcome} @ ${sig.price:.4f} [{src}] "
                            f"→ {result.order_id[:16]} │ EV:+{sig.ev_per_share*100:.1f}¢ │ {sig.score_line}"
                        )
                    else:
                        log(f"ORDER FAILED: {result.error} │ {sig.outcome}")

                # ── PAPER EXECUTION ──
                else:
                    if sig.cost <= bankroll:
                        bankroll -= sig.cost
                        pos = positions.open(sig)
                        src = "LIVE" if sig.price_source == "clob" else "GAMMA"
                        log(
                            f"PAPER {sig.shares}× {sig.outcome} @ ${sig.price:.4f} [{src}] "
                            f"→ {sig.implied_return:.1%} │ EV:+{sig.ev_per_share*100:.1f}¢ │ {sig.score_line}"
                        )

                # Push after each trade
                push_dashboard()

                # Add to dashboard trade history
                update_state(trade_history=[{
                    "id": sig.id, "engine": "harvest", "sport": sig.sport,
                    "event": sig.event_title, "outcome": sig.outcome,
                    "entryPrice": sig.price, "shares": sig.shares,
                    "cost": sig.cost, "confidence": sig.confidence,
                    "level": sig.level, "scoreLine": sig.score_line,
                    "priceSource": sig.price_source, "spread": sig.spread,
                    "ev": sig.ev_per_share, "status": "open", "pnl": None,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }] + (get_existing_trades()))

            push_dashboard()
            time.sleep(config.SCAN_INTERVAL)

        except Exception as e:
            log(f"ERROR: {e}")
            import traceback
            traceback.print_exc()
            time.sleep(30)


def get_existing_trades() -> list:
    """Get existing trade history from dashboard state."""
    from api import get_state
    return get_state().get("trade_history", [])[:299]


# ══════════════════════════════════════════════════
# STARTUP
# ══════════════════════════════════════════════════

def main():
    global bankroll

    print()
    print("▓" * 56)
    print("▓  SIGNAL HARVEST v3                                ▓")
    print("▓  Sports Blowout Harvester · Maker Limit Orders    ▓")
    print("▓" * 56)
    print()

    mode = "PAPER" if config.PAPER_MODE else "!! LIVE !!"
    print(f"  Mode:       {mode}")
    print(f"  Bankroll:   ${config.STARTING_BANKROLL:.2f}")
    print(f"  Sports:     {len(config.ESPN_SPORTS)}")
    print(f"  Price:      ${config.PRICE_MIN:.2f} – ${config.PRICE_MAX:.2f}")
    print(f"  Max spread: {config.MAX_SPREAD:.0%}")
    print(f"  Min EV:     ${config.MIN_EV:.3f}/share")
    print(f"  Kelly:      {config.KELLY_FRACTION}× (max {config.KELLY_MAX_PCT:.0%})")
    print(f"  Scan:       every {config.SCAN_INTERVAL}s")
    print()

    # Auth status
    clob = health_check()
    print(f"  CLOB read:  {'✓' if clob['read'] else '✗'}")
    print(f"  CLOB auth:  {'✓' if clob['auth'] else '✗ (paper only)'}")
    if not clob["auth"] and not config.PAPER_MODE:
        print("  !! LIVE MODE but no CLOB auth — will not place orders")
    print()
    print("─" * 56)

    # Load persisted state
    stats = positions.get_stats(bankroll)
    if stats["trades"] > 0:
        print(f"  Restored: {stats['trades']} trades, ${stats['pnl']:+.2f} P&L")

    # Start API
    os.makedirs(os.path.dirname(config.STATE_FILE) or "data", exist_ok=True)
    start_api()
    push_dashboard()

    if not config.PAPER_MODE:
        print()
        confirm = input("  LIVE MODE — type 'CONFIRM' to proceed: ")
        if confirm.strip() != "CONFIRM":
            print("  Aborted.")
            sys.exit(0)

    log(f"Engine started ({mode})")

    # Single engine loop
    try:
        engine_loop()
    except KeyboardInterrupt:
        stats = positions.get_stats(bankroll)
        print(f"\n\n  ═══ SESSION END ═══")
        print(f"  Equity:  ${stats['equity']:.2f}")
        print(f"  P&L:    ${stats['pnl']:+.2f} ({stats['roi']:+.1f}%)")
        print(f"  Trades: {stats['trades']} ({stats['win_rate']:.1%} win)")


if __name__ == "__main__":
    main()
