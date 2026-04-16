"""
edge.py — Engine 2: Edge Finder (Convergence Trade)
Buys Polymarket when ESPN sportsbook odds show mispricing.
EXITS BEFORE THE GAME STARTS — we capture the convergence, not the game risk.

Strategy:
  Polymarket = 67% accurate 1-7 days out, 96% accurate at game time.
  ESPN/FanDuel = ~95% accurate from the moment odds are posted.
  We buy the gap. Market corrects toward game time. We sell before tip-off.

Exit conditions (checked every 30 seconds):
  1. TAKE PROFIT: remaining edge < 2% (market corrected)
  2. PRE-GAME EXIT: 30 min before game start (avoid game risk)
  3. STOP LOSS: price dropped 5¢ below entry
  4. STALE: held > 48 hours with no convergence
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from espn import fetch_pregame_odds, GameOdds
from teams import generate_search_terms as team_search_terms, find_team_in_outcomes
from clob import ClobInterface, parse_market_tokens
from positions import PositionManager
from sizing import compute_bet_size
from config import (
    EDGE_MIN_EDGE, EDGE_MAX_PRICE, EDGE_MIN_PRICE,
    EDGE_MIN_HOURS_BEFORE, EDGE_MAX_HOURS_BEFORE,
    EDGE_EXIT_REMAINING, EDGE_STOP_LOSS,
    EDGE_PRE_GAME_EXIT_MIN, EDGE_STALE_HOURS,
    MIN_MARKET_LIQUIDITY, MIN_DAILY_VOLUME,
)

logger = logging.getLogger("edge")


@dataclass
class EdgeSignal:
    engine: str = "edge"
    sport: str = ""
    espn_id: str = ""
    condition_id: str = ""
    market_question: str = ""
    team: str = ""
    side: str = ""
    token_id: str = ""
    clob_price: float = 0.0
    true_prob: float = 0.0
    edge: float = 0.0
    provider: str = ""
    moneyline: int = 0
    bet_size: float = 0.0
    confidence: float = 0.0
    commence_time: str = ""      # ISO 8601 — when game starts


def _hours_until_game(commence_time: str) -> Optional[float]:
    """Parse ISO 8601 commence time and return hours until game."""
    if not commence_time:
        return None
    try:
        # Handle various ISO formats
        ct = commence_time.replace("Z", "+00:00")
        if "T" in ct:
            game_dt = datetime.fromisoformat(ct)
        else:
            return None
        now = datetime.now(timezone.utc)
        if game_dt.tzinfo is None:
            game_dt = game_dt.replace(tzinfo=timezone.utc)
        delta = (game_dt - now).total_seconds() / 3600
        return delta
    except Exception:
        return None


def _minutes_until_game(commence_time: str) -> Optional[float]:
    """Return minutes until game start."""
    hours = _hours_until_game(commence_time)
    return hours * 60 if hours is not None else None


async def scan_edge(clob: ClobInterface, positions: PositionManager) -> tuple[list[EdgeSignal], list[dict]]:
    """
    Scan for convergence trade opportunities.
    Returns (tradeable_signals, all_edges_for_dashboard).
    all_edges includes near-misses below threshold for the scanner view.
    """
    signals = []
    all_edges = []  # For dashboard — includes sub-threshold edges
    
    all_odds = await fetch_pregame_odds()
    if not all_odds:
        return [], []

    # Group by sport, deduplicate by game (prefer ESPN BET > FanDuel)
    by_sport: dict[str, list[GameOdds]] = {}
    for o in all_odds:
        by_sport.setdefault(o.sport, []).append(o)

    for sport, odds_list in by_sport.items():
        poly_events = await clob.fetch_polymarket_events(sport)
        if not poly_events:
            continue

        seen: set[str] = set()
        preferred = ["ESPN BET", "FanDuel", "BetMGM"]
        odds_list.sort(key=lambda o: preferred.index(o.provider) if o.provider in preferred else 99)

        for odds in odds_list:
            if odds.espn_id in seen:
                continue
            seen.add(odds.espn_id)

            # Check timing — only enter 2-48h before game
            hours = _hours_until_game(odds.commence_time)
            if hours is None:
                continue
            if hours < EDGE_MIN_HOURS_BEFORE or hours > EDGE_MAX_HOURS_BEFORE:
                continue

            # Try to match to Polymarket
            for ev in poly_events:
                parsed = parse_market_tokens(ev)
                if not parsed:
                    continue
                if parsed.get("liquidity", 0) < MIN_MARKET_LIQUIDITY:
                    continue
                if parsed.get("volume", 0) < MIN_DAILY_VOLUME:
                    continue
                if positions.has_position_for(parsed["condition_id"]):
                    continue

                outcomes = parsed["outcomes"]

                # Match both teams using robust fuzzy matcher
                hi = find_team_in_outcomes(odds.home_team, odds.home_abbrev, outcomes)
                ai = find_team_in_outcomes(odds.away_team, odds.away_abbrev, outcomes)

                if hi < 0 or ai < 0 or hi == ai:
                    continue

                # Check both sides
                for team, prob, ml, idx in [
                    (odds.home_team, odds.home_prob, odds.home_ml, hi),
                    (odds.away_team, odds.away_prob, odds.away_ml, ai),
                ]:
                    tid = parsed["token_ids"][idx]
                    poly_price = clob.get_price(tid, "BUY")
                    if poly_price is None or poly_price > EDGE_MAX_PRICE or poly_price < EDGE_MIN_PRICE:
                        continue

                    edge = prob - poly_price
                    
                    # Record ALL edges >= 1% for dashboard scanner (even if below trade threshold)
                    if edge >= 0.01:
                        all_edges.append({
                            "team": team, "sport": sport, "poly": poly_price,
                            "true": prob, "edge": edge, "provider": odds.provider,
                            "hours": round(hours, 1),
                        })

                    # Only trade if above minimum edge threshold
                    if edge < EDGE_MIN_EDGE:
                        continue

                    bet = compute_bet_size("edge", poly_price, prob, positions.equity, positions, sport=sport, edge=edge)
                    if bet <= 0:
                        continue

                    signals.append(EdgeSignal(
                        sport=sport, espn_id=odds.espn_id,
                        condition_id=parsed["condition_id"],
                        market_question=parsed["question"],
                        team=team, side=outcomes[idx], token_id=tid,
                        clob_price=poly_price, true_prob=prob, edge=edge,
                        provider=odds.provider, moneyline=ml,
                        bet_size=bet, confidence=prob,
                        commence_time=odds.commence_time,
                    ))
                break  # one poly match per ESPN game

    # Sort all_edges by edge size descending
    all_edges.sort(key=lambda e: e["edge"], reverse=True)

    for s in signals:
        h = _hours_until_game(s.commence_time)
        logger.info(
            f"⚡ EDGE: {s.team} @{s.clob_price:.3f} true={s.true_prob:.3f} "
            f"edge={s.edge:.3f} [{s.provider}] ${s.bet_size} | game in {h:.1f}h"
        )
    return signals, all_edges[:20]


async def check_edge_exits(clob: ClobInterface, positions: PositionManager):
    """
    Check all Edge Finder positions for exit conditions.
    Runs every 30 seconds — pre-game exit is time-critical.

    Priority:
      1. PRE-GAME EXIT: 30 min before game (highest priority — avoid game risk)
      2. TAKE PROFIT: remaining edge < 2%
      3. STOP LOSS: price dropped 5¢
      4. STALE: held > 48 hours
    """
    for pos in positions.get_filled_by_engine("edge"):
        current_price = clob.get_price(pos.token_id, "SELL")
        if current_price is None:
            continue

        entry = pos.fill_price or pos.entry_price
        age_hours = (time.time() - pos.opened_at) / 3600

        # 1. PRE-GAME EXIT — highest priority
        minutes_to_game = _minutes_until_game(pos.game_start_time)
        if minutes_to_game is not None and minutes_to_game <= EDGE_PRE_GAME_EXIT_MIN:
            pnl_preview = (current_price - entry) * pos.size
            logger.info(
                f"⏰ PRE-GAME EXIT: {pos.team} | game in {minutes_to_game:.0f}min | "
                f"entry={entry:.3f} now={current_price:.3f} pnl=${pnl_preview:+.2f}"
            )
            await _do_exit(clob, positions, pos, current_price, "pre_game_exit")
            continue

        # 2. TAKE PROFIT — edge consumed by market convergence
        remaining_edge = pos.true_prob - current_price
        if remaining_edge < EDGE_EXIT_REMAINING and current_price > entry:
            logger.info(f"💰 TAKE PROFIT: {pos.team} entry={entry:.3f} now={current_price:.3f} remaining_edge={remaining_edge:.3f}")
            await _do_exit(clob, positions, pos, current_price, "take_profit")
            continue

        # 3. STOP LOSS — model was wrong or new info arrived
        if current_price < entry - EDGE_STOP_LOSS:
            logger.info(f"🛑 STOP LOSS: {pos.team} entry={entry:.3f} now={current_price:.3f}")
            await _do_exit(clob, positions, pos, current_price, "stop_loss")
            continue

        # 4. STALE — no convergence after 48h
        if age_hours > EDGE_STALE_HOURS:
            logger.info(f"⏳ STALE EXIT: {pos.team} held {age_hours:.1f}h, entry={entry:.3f} now={current_price:.3f}")
            await _do_exit(clob, positions, pos, current_price, "stale_exit")
            continue


async def _do_exit(clob, positions, pos, price, reason):
    """Execute an exit — place SELL order and record."""
    if clob.is_authenticated():
        result = clob.place_order(pos.token_id, price, pos.size, "SELL")
        if not result:
            logger.warning(f"Exit SELL failed for {pos.team}")
            return
    await positions.exit_position(pos.id, price, reason)
