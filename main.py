"""
main.py v2 — Signal Harvest + Synth Bot

Changes from v1:
- Smart scan timing (sleeps until T-20s before window close)
- EV displayed in trade logs
- Harvest trades pass CLOB price fields
- Arb trades handled in synth engine
"""

import time
import sys
import os
import json
import threading
from datetime import datetime, timezone
from dataclasses import asdict

import config
from espn import fetch_verified_games
from markets import scan_for_harvests
from crypto import (scan_crypto_opportunities, get_synth_status,
                     start_binance_feed, get_next_scan_delay)
from compound import BankrollManager
from api_server import (start_api, update, set_games, set_harvest_targets,
                         set_synth_signals, add_trade, add_log)

bank = BankrollManager()
bank_lock = threading.Lock()
cycle_count = {"harvest": 0, "synth": 0}


def log(msg, engine=""):
    tag = f"[{engine.upper()}] " if engine else ""
    print(f"  {tag}{msg}")
    add_log(f"{tag}{msg}")


def push_state():
    with bank_lock:
        stats = bank.get_stats()
        positions = bank.get_open()
    update(
        bankroll=stats["bankroll"], starting=stats["starting"],
        pnl=stats["pnl"], equity=stats["equity"],
        trades=stats["trades"], wins=stats["wins"],
        win_rate=stats["win_rate"], open_count=stats["open"],
        exposure=stats["exposure"],
        harvest_exposure=stats["harvest_exposure"],
        synth_exposure=stats["synth_exposure"],
        drawdown=stats["drawdown"],
        harvest_trades=stats["harvest_trades"],
        synth_trades=stats["synth_trades"],
        scan_count=cycle_count["harvest"] + cycle_count["synth"],
        open_positions=[asdict(p) for p in positions],
    )


# ═══════════════════════════════════════════════
# ENGINE 1: HARVEST
# ═══════════════════════════════════════════════
def harvest_loop():
    while True:
        try:
            cycle_count["harvest"] += 1
            now = datetime.now(timezone.utc)
            update(scanning=True, last_scan=now.isoformat())

            with bank_lock:
                resolved = bank.check_resolutions(engine="harvest")
            if resolved:
                for pos in resolved:
                    icon = "✓" if pos.pnl > 0 else "✗"
                    log(f"{icon} {pos.event} → ${pos.pnl:+.2f} ({pos.status})", "harvest")
                    add_trade(_trade_dict(pos))

            games = fetch_verified_games()
            live_games = [g for g in games if g.level != "final"]
            set_games([{
                "sport": g.sport, "home": g.home_abbrev, "away": g.away_abbrev,
                "homeScore": g.home_score, "awayScore": g.away_score,
                "leader": g.leader_team, "lead": g.lead,
                "period": g.period, "clock": g.clock,
                "elapsed": g.elapsed_pct, "confidence": g.confidence,
                "level": g.level, "scoreLine": g.score_line,
            } for g in games])

            if live_games:
                log(f"{len(live_games)} live verified games ({len(games)} total)", "harvest")
            else:
                log(f"No live games ({len(games)} final)", "harvest")

            with bank_lock:
                eq = bank.get_equity()
                h_exp = bank.get_engine_exposure("harvest")
            targets = scan_for_harvests(live_games, eq, h_exp)
            set_harvest_targets([{
                "id": t.id, "sport": t.sport, "event": t.event_title,
                "outcome": t.outcome, "price": t.price, "shares": t.shares,
                "cost": t.cost, "return": t.implied_return,
                "confidence": t.confidence, "level": t.level,
                "scoreLine": t.score_line, "leader": t.leader_team,
                "priceSource": t.price_source, "spread": t.spread,
                "ev": t.ev,
            } for t in targets])

            for t in targets[:8]:
                with bank_lock:
                    ok, reason = bank.can_trade(t.cost, t.condition_id, "harvest")
                    if not ok:
                        continue
                    bank.open_position(
                        signal_id=t.id, engine="harvest", sport=t.sport,
                        event=t.event_title, outcome=t.outcome, side=t.side,
                        token_id=t.token_id, condition_id=t.condition_id,
                        entry_price=t.price, shares=t.shares,
                        confidence=t.confidence, level=t.level,
                        detail=t.score_line,
                    )
                src = "LIVE" if t.price_source == "clob" else "GAMMA"
                log(f"HARVEST {t.shares}× {t.outcome} @ ${t.price:.4f} [{src}] → {t.implied_return:.1%} EV:{t.ev:+.4f} │ {t.score_line}", "harvest")
                add_trade({"id": t.id, "engine": "harvest", "sport": t.sport,
                           "event": t.event_title, "outcome": t.outcome,
                           "entryPrice": t.price, "shares": t.shares,
                           "cost": t.cost, "return": t.implied_return,
                           "confidence": t.confidence, "level": t.level,
                           "scoreLine": t.score_line, "status": "open",
                           "pnl": None, "timestamp": now.isoformat(),
                           "priceSource": t.price_source, "spread": t.spread,
                           "ev": t.ev})

            push_state()
            update(scanning=False, engines={**_engines()})
            time.sleep(config.HARVEST_SCAN_INTERVAL)

        except Exception as e:
            log(f"Error: {e}", "harvest")
            import traceback; traceback.print_exc()
            time.sleep(30)


# ═══════════════════════════════════════════════
# ENGINE 2: SYNTH (with smart scan timing)
# ═══════════════════════════════════════════════
def synth_loop():
    while True:
        try:
            cycle_count["synth"] += 1
            now = datetime.now(timezone.utc)

            with bank_lock:
                eq = bank.get_equity()
                s_exp = bank.get_engine_exposure("synth")

            with bank_lock:
                resolved = bank.check_resolutions(engine="synth")
            if resolved:
                for pos in resolved:
                    if pos.engine == "synth":
                        icon = "✓" if pos.pnl > 0 else "✗"
                        log(f"{icon} {pos.event} → ${pos.pnl:+.2f} ({pos.status})", "synth")
                        add_trade(_trade_dict(pos))

            signals = scan_crypto_opportunities(eq, s_exp)
            set_synth_signals([{
                "id": s.id, "asset": s.asset, "timeframe": s.timeframe,
                "direction": s.direction, "synthProb": s.synth_prob_up,
                "polyProb": s.poly_prob_up, "edge": s.edge,
                "edgePct": round(s.edge * 100, 1), "confidence": s.confidence,
                "price": s.price, "side": s.side,
                "eventEnd": s.event_end, "currentPrice": 0,
                "shares": s.shares, "cost": s.cost,
                "timestamp": s.timestamp,
                "priceSource": s.price_source,
                "realPrice": s.real_price,
                "midpoint": s.midpoint,
                "spread": s.spread,
                "ev": s.ev,
                "layer": s.layer,
            } for s in signals])

            if signals:
                log(f"{len(signals)} signals ({', '.join(s.layer for s in signals)})", "synth")

            for sig in signals:
                with bank_lock:
                    ok, reason = bank.can_trade(sig.cost, sig.slug or sig.id, "synth")
                    if not ok:
                        continue
                    bank.open_position(
                        signal_id=sig.id, engine="synth", sport=sig.asset,
                        event=f"{sig.asset} {sig.timeframe} {sig.direction}",
                        outcome=f"{sig.asset} {sig.direction.upper()}",
                        side=sig.side, token_id=sig.token_id,
                        condition_id=sig.slug or sig.id,
                        entry_price=sig.price, shares=sig.shares,
                        confidence=sig.confidence,
                        level=sig.layer,
                        detail=sig.detail,
                    )
                source_tag = "LIVE" if sig.price_source == "clob" else "EST"
                log(f"{sig.layer.upper()} {sig.asset} {sig.timeframe} {sig.direction.upper()} @ ${sig.price:.4f} [{source_tag}] │ EV:{sig.ev:+.4f} │ Δ:{sig.window_delta_pct:+.3f}%", "synth")
                add_trade({"id": sig.id, "engine": "synth", "sport": sig.asset,
                           "event": f"{sig.asset} {sig.timeframe}",
                           "outcome": f"{sig.direction.upper()}", "side": sig.side,
                           "entryPrice": sig.price, "shares": sig.shares,
                           "cost": sig.cost, "edge": sig.edge,
                           "edgePct": round(sig.edge * 100, 1),
                           "confidence": sig.confidence,
                           "synthProb": sig.synth_prob_up, "polyProb": sig.poly_prob_up,
                           "timeframe": sig.timeframe, "status": "open",
                           "pnl": None, "timestamp": now.isoformat(),
                           "priceSource": sig.price_source,
                           "realPrice": sig.real_price,
                           "midpoint": sig.midpoint,
                           "spread": sig.spread,
                           "ev": sig.ev,
                           "layer": sig.layer,
                           })

            push_state()
            update(engines={**_engines()})

            # ── Smart scan timing ──
            delay = get_next_scan_delay()
            time.sleep(delay)

        except Exception as e:
            log(f"Error: {e}", "synth")
            import traceback; traceback.print_exc()
            time.sleep(10)


def _trade_dict(pos) -> dict:
    return {"id": pos.signal_id, "engine": pos.engine, "sport": pos.sport,
            "event": pos.event, "outcome": pos.outcome, "side": pos.side,
            "entryPrice": pos.entry_price, "exitPrice": pos.exit_price,
            "shares": pos.shares, "cost": pos.cost_basis,
            "confidence": pos.confidence, "level": pos.level,
            "detail": pos.detail, "status": pos.status,
            "pnl": pos.pnl, "timestamp": pos.resolved_time or pos.entry_time}


def _engines():
    return {"harvest": config.HARVEST_ENABLED, "synth": config.SYNTH_ENABLED}


def main():
    print()
    print("▓" * 64)
    print("▓  SIGNAL │ HARVEST + SYNTH  v2                              ▓")
    print("▓  Dual-Engine Compound Machine                              ▓")
    print("▓" * 64)
    print()
    mode = "PAPER" if config.PAPER_MODE else "!! LIVE !!"
    print(f"  Mode:       {mode}")
    print(f"  Bankroll:   ${config.STARTING_BANKROLL:.2f}")
    print(f"  Kelly:      {config.KELLY_FRACTION}× (max {config.KELLY_MAX_BET_PCT:.0%}/trade)")
    print()
    print(f"  ENGINE 1 — HARVEST (ESPN Sports)")
    print(f"    Status:   {'ON' if config.HARVEST_ENABLED else 'OFF'}")
    print(f"    Sports:   {len(config.ESPN_SPORTS)}")
    print(f"    CLOB:     {'Real prices' if config.HARVEST_USE_CLOB else 'Gamma only'}")
    print(f"    Interval: {config.HARVEST_SCAN_INTERVAL}s")
    print()
    synth_st = get_synth_status()
    print(f"  ENGINE 2 — SYNTH (Bittensor SN50)")
    print(f"    Status:   {'ON' if config.SYNTH_ENABLED else 'OFF'} │ {synth_st['message']}")
    print(f"    Assets:   {', '.join(config.SYNTH_ASSETS)}")
    print(f"    Timing:   Smart (T-{config.SNIPE_LEAD_TIME}s wake, {config.SNIPE_SCAN_BURST}s burst)")
    print(f"    Arb:      {'ON' if config.ARB_ENABLED else 'OFF'}")
    print(f"    Resolve:  {config.CRYPTO_RESOLVE_BUFFER}s buffer")
    print()
    print("─" * 64)

    os.makedirs(config.LOG_DIR, exist_ok=True)
    start_api()
    try:
        start_binance_feed()
    except Exception as e:
        print(f"  [WARN] Binance feed failed: {e}")
    push_state()
    update(engines=_engines())

    if config.PAPER_MODE:
        log("Paper mode — no real trades")
    else:
        confirm = input("  LIVE MODE — type 'CONFIRM': ")
        if confirm.strip() != "CONFIRM":
            sys.exit(0)

    threads = []

    if config.HARVEST_ENABLED:
        t1 = threading.Thread(target=harvest_loop, daemon=True, name="harvest")
        t1.start()
        threads.append(t1)
        log("Harvest engine started (Kelly + CLOB)", "harvest")

    if config.SYNTH_ENABLED:
        t2 = threading.Thread(target=synth_loop, daemon=True, name="synth")
        t2.start()
        threads.append(t2)
        log("Crypto engine started (EV + Kelly + smart timing + arb)", "synth")

    if not threads:
        log("No engines enabled!")
        sys.exit(1)

    try:
        while True:
            time.sleep(60)
            with bank_lock:
                stats = bank.get_stats()
            print(f"\n  ── {datetime.now(timezone.utc).strftime('%H:%M')} UTC │ "
                  f"Equity: ${stats['equity']:.2f} │ P&L: ${stats['pnl']:+.2f} │ "
                  f"Open: {stats['open']} │ WR: {stats['win_rate']:.1%} │ "
                  f"H:{stats['harvest_trades']} S:{stats['synth_trades']}")
    except KeyboardInterrupt:
        with bank_lock:
            stats = bank.get_stats()
        print(f"\n\n  ═══ SESSION END ═══")
        print(f"  Equity:  ${stats['equity']:.2f}")
        print(f"  P&L:    ${stats['pnl']:+.2f} ({stats['roi']:+.1f}%)")
        print(f"  Trades: {stats['trades']} ({stats['win_rate']:.1%} win)")


if __name__ == "__main__":
    main()
