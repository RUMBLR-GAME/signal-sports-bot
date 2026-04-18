"""
edge.py — Engine 2: Edge Finder / Convergence Trade (v18)

Changes from v17:
  • Accepts combined pregame odds list (ESPN for US + Odds API for soccer)
  • Uses match_game_to_market (which applies ambiguity rule)
  • New sizing signature (returns reason, used for diagnostics)
  • Stale-quote penalty on edge (reduces effective edge, not hard block)
  • Exit path uses new positions.exit_position semantics
"""
import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, List, Tuple

from espn import GameOdds
from teams import match_game_to_market, find_team_in_outcomes
from clob import ClobInterface, parse_market_tokens
from positions import PositionManager
from sizing import compute_bet_size
from config import (
    EDGE_MIN_EDGE, EDGE_MAX_PRICE, EDGE_MIN_PRICE,
    EDGE_MIN_HOURS_BEFORE, EDGE_MAX_HOURS_BEFORE,
    EDGE_EXIT_REMAINING, EDGE_STOP_LOSS,
    EDGE_PRE_GAME_EXIT_MIN, EDGE_STALE_HOURS,
    MIN_MARKET_LIQUIDITY, MIN_DAILY_VOLUME,
    POLY_STALE_QUOTE_SEC, POLY_STALE_PENALTY,
    MAX_TOTAL_EXPOSURE_PCT, MAX_OPEN_POSITIONS, STARTING_BANKROLL,
)
from harvest import _market_is_stale

logger = logging.getLogger("edge")


@dataclass
class EdgeSignal:
    engine: str = "edge"
    sport: str = ""
    espn_id: str = ""
    condition_id: str = ""
    market_question: str = ""
    team: str = ""
    bet_outcome: str = ""
    outcome_idx: int = 0
    token_id: str = ""
    clob_price: float = 0.0
    true_prob: float = 0.0
    edge: float = 0.0
    provider: str = ""
    moneyline: int = 0
    bet_size: float = 0.0
    confidence: float = 0.0
    commence_time: str = ""
    sizing_reason: str = ""


def _hours_until(commence_time: str) -> Optional[float]:
    if not commence_time:
        return None
    try:
        ct = commence_time.replace("Z", "+00:00")
        game_dt = datetime.fromisoformat(ct)
        if game_dt.tzinfo is None:
            game_dt = game_dt.replace(tzinfo=timezone.utc)
        return (game_dt - datetime.now(timezone.utc)).total_seconds() / 3600.0
    except Exception:
        return None


def _ts_until(commence_time: str) -> Optional[float]:
    if not commence_time:
        return None
    try:
        ct = commence_time.replace("Z", "+00:00")
        return datetime.fromisoformat(ct).timestamp()
    except Exception:
        return None


async def scan_edge(
    clob: ClobInterface, positions: PositionManager,
    all_odds: List[GameOdds],
    lineup_watcher=None,
) -> Tuple[List[EdgeSignal], List[dict]]:
    """
    all_odds: combined list of GameOdds from ESPN + Odds API.
    lineup_watcher: optional LineupWatcher instance. When provided, its
      signals shift effective edge for matches where team news has landed.
    Returns (signals, all_edges_for_dashboard).
    """
    signals: List[EdgeSignal] = []
    all_edges: List[dict] = []

    if not all_odds:
        return signals, all_edges

    # Group by sport. Prefer sharpest / non-duplicate provider per game.
    by_sport: dict = {}
    for o in all_odds:
        by_sport.setdefault(o.sport, []).append(o)

    for sport, odds_list in by_sport.items():
        # Fetch Polymarket events for this sport (once)
        events = await clob.fetch_polymarket_events(sport)
        if not events:
            continue

        # Dedupe by game — pick sharpest provider available
        preferred = ["Pinnacle", "Betfair", "Bet365", "ESPN BET", "FanDuel", "BetMGM"]
        odds_list.sort(key=lambda o: preferred.index(o.provider) if o.provider in preferred else 99)
        seen: set = set()

        for odds in odds_list:
            # Portfolio-wide caps: don't add new positions when we're at exposure limit.
            # IMPORTANT: include signals generated earlier in THIS scan as projected exposure,
            # else a single scan can deploy 100% of capital in one burst.
            open_cost = sum(
                p.cost for p in positions.positions.values()
                if p.status in ("open", "filled")
            )
            projected_cost = open_cost + sum(s.bet_size for s in signals)
            exposure_pct = projected_cost / max(STARTING_BANKROLL, 1)
            if exposure_pct >= MAX_TOTAL_EXPOSURE_PCT:
                logger.debug(
                    f"EDGE: skipping — projected exposure {exposure_pct:.0%} >= cap {MAX_TOTAL_EXPOSURE_PCT:.0%}"
                )
                return signals, all_edges
            total_positions = len(positions.positions) + len(signals)
            if total_positions >= MAX_OPEN_POSITIONS:
                logger.debug(
                    f"EDGE: skipping — projected {total_positions} >= cap {MAX_OPEN_POSITIONS}"
                )
                return signals, all_edges

            key = (odds.home_team.lower(), odds.away_team.lower(), odds.commence_time[:10])
            if key in seen:
                continue
            seen.add(key)

            hours = _hours_until(odds.commence_time)
            if hours is None or hours < EDGE_MIN_HOURS_BEFORE or hours > EDGE_MAX_HOURS_BEFORE:
                continue

            # Find Polymarket market
            parsed = None
            for ev in events:
                p = parse_market_tokens(ev)
                if not p:
                    continue
                if p.get("liquidity", 0) < MIN_MARKET_LIQUIDITY:
                    continue
                if p.get("volume", 0) < MIN_DAILY_VOLUME:
                    continue
                hi, ai = match_game_to_market(
                    odds.home_team, odds.home_abbrev,
                    odds.away_team, odds.away_abbrev,
                    p["question"], p["outcomes"],
                )
                if hi < 0 or ai < 0:
                    continue
                p["_home_idx"] = hi
                p["_away_idx"] = ai
                parsed = p
                break

            if not parsed:
                continue
            # Check all condition_ids for this event (soccer has multiple —
            # home-win, away-win, draw are separate markets, must not trade both).
            cids_to_check = parsed.get("_condition_ids") or [parsed["condition_id"]]
            if any(positions.has_position_for(c) for c in cids_to_check):
                continue
            # Belt-and-braces: also reject if we already have an edge position
            # on either team (same game = one bet maximum).
            if positions.has_position_for_game(
                odds.home_team, odds.away_team, engine="edge"
            ):
                continue

            hi = parsed["_home_idx"]
            ai = parsed["_away_idx"]
            stale = _market_is_stale(parsed)

            # Consult lineup watcher (if enabled) for team-news edge shift
            lineup_sig = None
            lineup_shift_home = 0.0
            lineup_shift_away = 0.0
            if lineup_watcher is not None:
                lineup_sig = lineup_watcher.get_signal(odds.home_team, odds.away_team)
                if lineup_sig:
                    # home_impact negative means home weakened → home true_prob ↓, away ↑
                    h_imp = lineup_sig.get("home_impact", 0.0)
                    a_imp = lineup_sig.get("away_impact", 0.0)
                    conf = lineup_sig.get("confidence", 0.0)
                    # Scale by confidence
                    lineup_shift_home = h_imp * conf
                    lineup_shift_away = a_imp * conf

            # Evaluate both sides BUT only trade the one with larger edge.
            # Taking both sides of the same match guarantees a loss on draw
            # and costs the spread on either result. One bet per game max.
            sides = [
                (odds.home_team, odds.home_prob + lineup_shift_home, odds.home_ml, hi, lineup_shift_home),
                (odds.away_team, odds.away_prob + lineup_shift_away, odds.away_ml, ai, lineup_shift_away),
            ]
            # Fetch prices + compute edges for both first
            side_edges = []
            for team, true_prob, ml, idx, lineup_adj in sides:
                true_prob = max(0.02, min(0.98, true_prob))
                tid = parsed["token_ids"][idx]
                poly_price = await clob.get_price(tid, "BUY")
                if poly_price is None:
                    try:
                        poly_price = float(parsed["prices"][idx])
                    except Exception:
                        poly_price = None
                if poly_price is None:
                    continue
                raw_edge = true_prob - poly_price
                eff_edge = raw_edge * POLY_STALE_PENALTY if stale else raw_edge
                side_edges.append({
                    "team": team, "true_prob": true_prob, "ml": ml, "idx": idx,
                    "tid": tid, "poly_price": poly_price,
                    "raw_edge": raw_edge, "eff_edge": eff_edge,
                    "lineup_adj": lineup_adj,
                })

            # Log ALL edges for diagnostics
            for se in side_edges:
                all_edges.append({
                    "team": se["team"], "sport": sport,
                    "poly": se["poly_price"], "true": se["true_prob"],
                    "edge": se["raw_edge"], "effective_edge": se["eff_edge"],
                    "provider": odds.provider, "moneyline": se["ml"],
                    "hours": round(hours, 1),
                    "stale": stale,
                    "lineup_adj": round(se["lineup_adj"], 4) if se["lineup_adj"] else 0,
                    "lineup_detail": (lineup_sig or {}).get("detail", "") if se["lineup_adj"] else "",
                    "commence_time": odds.commence_time,
                    "liquidity": parsed.get("liquidity", 0),
                })

            # Pick only the side with LARGEST effective edge
            tradable = [se for se in side_edges if se["eff_edge"] >= EDGE_MIN_EDGE
                        and EDGE_MIN_PRICE <= se["poly_price"] <= EDGE_MAX_PRICE]
            if not tradable:
                continue
            best = max(tradable, key=lambda x: x["eff_edge"])

            # Size it
            team = best["team"]
            true_prob = best["true_prob"]
            ml = best["ml"]
            idx = best["idx"]
            tid = best["tid"]
            poly_price = best["poly_price"]
            raw_edge = best["raw_edge"]
            eff_edge = best["eff_edge"]
            lineup_adj = best["lineup_adj"]

            start_ts = _ts_until(odds.commence_time)
            equity = positions.equity
            size, sz_reason = compute_bet_size(
                "edge", poly_price, true_prob, equity, positions,
                sport=sport, edge=eff_edge, game_start_ts=start_ts,
                pending_signals=signals,
            )
            if size <= 0:
                continue

            signals.append(EdgeSignal(
                sport=sport, espn_id=odds.espn_id,
                    condition_id=parsed["condition_id"],
                    market_question=parsed["question"],
                    team=team,
                    bet_outcome=parsed["outcomes"][idx],
                    outcome_idx=idx,
                    token_id=tid, clob_price=poly_price,
                    true_prob=true_prob, edge=raw_edge,
                    provider=odds.provider, moneyline=ml,
                    bet_size=size, confidence=true_prob,
                    commence_time=odds.commence_time,
                    sizing_reason=sz_reason,
            ))
            logger.info(
                f"EDGE [{sport}]: {team} @{poly_price:.3f} true={true_prob:.3f} "
                f"edge={raw_edge:.3f}{' (stale→'+format(eff_edge,'.3f')+')' if stale else ''} "
                f"[{odds.provider}] ${size:.2f} | T-{hours:.1f}h"
            )

    all_edges.sort(key=lambda e: e["edge"], reverse=True)
    return signals, all_edges[:300]


async def check_edge_exits(clob: ClobInterface, positions: PositionManager):
    """
    Priority: pre-game exit > take-profit > stop-loss > stale.
    Updates position mark-to-market prices along the way.
    """
    for pos in positions.get_filled_by_engine("edge"):
        current = await clob.get_price(pos.token_id, "SELL")
        if current is None:
            continue
        positions.mark_current_price(pos.id, current)

        entry = pos.fill_price or pos.entry_price
        age_hours = (time.time() - pos.opened_at) / 3600.0

        # 1. Pre-game exit
        mins_to_game = None
        if pos.game_start_time:
            start_ts = _ts_until(pos.game_start_time)
            if start_ts is not None:
                mins_to_game = (start_ts - time.time()) / 60.0
        if mins_to_game is not None and mins_to_game <= EDGE_PRE_GAME_EXIT_MIN:
            await _do_exit(clob, positions, pos, current, f"pre_game (T-{mins_to_game:.0f}m)")
            continue

        # 2. Take profit: remaining edge consumed
        remaining_edge = pos.true_prob - current
        if remaining_edge < EDGE_EXIT_REMAINING and current > entry:
            await _do_exit(clob, positions, pos, current, f"take_profit (edge left {remaining_edge:.3f})")
            continue

        # 3. Stop loss
        if current < entry - EDGE_STOP_LOSS:
            await _do_exit(clob, positions, pos, current, f"stop_loss @{current:.3f}")
            continue

        # 4. Stale
        if age_hours > EDGE_STALE_HOURS:
            await _do_exit(clob, positions, pos, current, f"stale (held {age_hours:.1f}h)")
            continue


async def _do_exit(clob, positions, pos, price, reason):
    if clob.is_authenticated():
        ok = await clob.place_order(pos.token_id, price, pos.size, "SELL")
        if not ok:
            logger.warning(f"Exit SELL failed for {pos.team}")
            return
    await positions.exit_position(pos.id, price, reason)
