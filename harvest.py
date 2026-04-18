"""
harvest.py — Engine 1: Blowout Harvest (v18)

Flow:
  1. Get verified blowouts from ESPN (or Sports WS when available).
  2. For each blowout, find matching Polymarket market.
  3. Apply filters: confidence, liquidity, price range, edge.
  4. Apply stale-quote penalty if last trade on market is old.
  5. Size with sport multiplier, drawdown governor, correlation caps.
  6. Emit signal + full diagnostic log for dashboard.
"""
import logging
import time
from dataclasses import dataclass
from typing import Optional, List, Tuple

from espn import VerifiedGame
from teams import match_game_to_market, normalize
from clob import ClobInterface, parse_market_tokens
from positions import PositionManager
from sizing import compute_bet_size
from config import (
    HARVEST_MIN_CONFIDENCE, HARVEST_MAX_PRICE, HARVEST_MIN_PRICE,
    HARVEST_MIN_EDGE, MIN_MARKET_LIQUIDITY,
    POLY_STALE_QUOTE_SEC, POLY_STALE_PENALTY,
    MAX_TOTAL_EXPOSURE_PCT, MAX_OPEN_POSITIONS, STARTING_BANKROLL,
)

logger = logging.getLogger("harvest")


@dataclass
class HarvestSignal:
    engine: str = "harvest"
    sport: str = ""
    game: Optional[VerifiedGame] = None
    condition_id: str = ""
    market_question: str = ""
    team: str = ""
    bet_outcome: str = ""
    outcome_idx: int = 0
    token_id: str = ""
    clob_price: float = 0.0
    confidence: float = 0.0
    effective_confidence: float = 0.0
    edge: float = 0.0
    bet_size: float = 0.0
    sizing_reason: str = ""
    score_line: str = ""


def _market_is_stale(parsed: dict, now: float = None) -> bool:
    lt = parsed.get("last_trade_time")
    if not lt:
        return True
    try:
        ts = float(lt)
        if ts > 1e12:
            ts /= 1000.0
        return (now or time.time()) - ts > POLY_STALE_QUOTE_SEC
    except (TypeError, ValueError):
        return True


async def scan_harvest(
    clob: ClobInterface, positions: PositionManager, blowouts: List[VerifiedGame]
) -> Tuple[List[HarvestSignal], List[dict]]:
    """
    blowouts: VerifiedGame list (from ESPN or WS-enriched source).
    Returns (signals, blowout_log). blowout_log has entry for EVERY blowout
    explaining whether it traded and why not (dashboard diagnostic).
    """
    signals: List[HarvestSignal] = []
    diag: List[dict] = []

    cache: dict = {}  # sport → polymarket events

    for game in blowouts:
        # Portfolio-wide caps
        open_cost = sum(
            p.cost for p in positions.positions.values()
            if p.status in ("open", "filled")
        )
        exposure_pct = open_cost / max(STARTING_BANKROLL, 1)
        if exposure_pct >= MAX_TOTAL_EXPOSURE_PCT:
            diag.append({
                "sport": game.sport, "leader": game.leader_abbrev, "lead": game.lead,
                "confidence": game.confidence, "score_line": game.score_line,
                "status": "skip", "reason": f"exposure cap {exposure_pct:.0%}",
            })
            break
        if len(positions.positions) >= MAX_OPEN_POSITIONS:
            diag.append({
                "sport": game.sport, "leader": game.leader_abbrev, "lead": game.lead,
                "confidence": game.confidence, "score_line": game.score_line,
                "status": "skip", "reason": f"position count cap {MAX_OPEN_POSITIONS}",
            })
            break

        entry = {
            "sport": game.sport, "leader": game.leader_abbrev,
            "trailer": game.trailer_abbrev, "lead": game.lead,
            "confidence": game.confidence, "score_line": game.score_line,
            "home": game.home_team, "away": game.away_team,
            "status": "pending", "reason": "",
        }

        if game.confidence < HARVEST_MIN_CONFIDENCE:
            entry.update(status="skip", reason=f"conf {game.confidence:.3f} < {HARVEST_MIN_CONFIDENCE}")
            diag.append(entry)
            continue

        # Already positioned on this game?
        if positions.has_position_for_game(game.home_team, game.away_team, engine="harvest"):
            entry.update(status="skip", reason="already positioned on this game")
            diag.append(entry)
            continue

        # Fetch Polymarket events (cached per sport this scan)
        if game.sport not in cache:
            cache[game.sport] = await clob.fetch_polymarket_events(game.sport)
        events = cache[game.sport]
        if not events:
            entry.update(status="skip", reason=f"no Polymarket events for {game.sport}")
            diag.append(entry)
            continue

        # Find matching market
        matched = None
        for ev in events:
            parsed = parse_market_tokens(ev)
            if not parsed or parsed.get("liquidity", 0) < MIN_MARKET_LIQUIDITY:
                continue
            hi, ai = match_game_to_market(
                game.home_team, game.home_abbrev,
                game.away_team, game.away_abbrev,
                parsed["question"], parsed["outcomes"],
            )
            if hi < 0 or ai < 0:
                continue
            # Which outcome is the LEADER?
            from teams import find_team_in_outcomes
            leader_idx = find_team_in_outcomes(game.leader, game.leader_abbrev, parsed["outcomes"])
            if leader_idx < 0:
                continue
            parsed["leader_idx"] = leader_idx
            matched = parsed
            break

        if not matched:
            entry.update(status="skip", reason="no matching Polymarket market (matcher rejected)")
            diag.append(entry)
            continue

        if positions.has_position_for(matched["condition_id"]):
            entry.update(status="skip", reason="already positioned on market")
            diag.append(entry)
            continue

        # Get live BUY price
        tid = matched["token_ids"][matched["leader_idx"]]
        price = await clob.get_price(tid, "BUY")
        if price is None:
            # Fallback to outcome_prices snapshot
            try:
                price = float(matched["prices"][matched["leader_idx"]])
            except Exception:
                price = None
        if price is None:
            entry.update(status="skip", reason="price unavailable")
            diag.append(entry)
            continue

        entry["price"] = price

        # Price gates
        if price > HARVEST_MAX_PRICE:
            entry.update(status="skip", reason=f"price {price:.3f} > max {HARVEST_MAX_PRICE}")
            diag.append(entry)
            continue
        if price < HARVEST_MIN_PRICE:
            entry.update(status="skip", reason=f"price {price:.3f} < min {HARVEST_MIN_PRICE}")
            diag.append(entry)
            continue

        edge = game.confidence - price
        if edge < HARVEST_MIN_EDGE:
            entry.update(status="skip", reason=f"edge {edge:.3f} < {HARVEST_MIN_EDGE}")
            diag.append(entry)
            continue
        entry["edge"] = edge

        # Stale-quote penalty — reduces effective confidence, not gate
        effective_conf = game.confidence
        stale = _market_is_stale(matched)
        if stale:
            # Penalize the edge, not the raw confidence
            effective_conf = price + (game.confidence - price) * POLY_STALE_PENALTY

        # Size it — pass in signals queued this scan to prevent cap race
        equity = positions.equity
        size, sz_reason = compute_bet_size(
            "harvest", price, effective_conf, equity, positions,
            sport=game.sport,
            pending_signals=signals,
        )
        if size <= 0:
            entry.update(status="skip", reason=f"sizing: {sz_reason}")
            diag.append(entry)
            continue
        entry.update(status="signal", reason="TRADING", bet=size, sizing=sz_reason)
        diag.append(entry)

        leader_outcome = matched["outcomes"][matched["leader_idx"]]
        signals.append(HarvestSignal(
            sport=game.sport, game=game,
            condition_id=matched["condition_id"],
            market_question=matched["question"],
            team=game.leader,
            bet_outcome=leader_outcome,
            outcome_idx=matched["leader_idx"],
            token_id=tid, clob_price=price,
            confidence=game.confidence,
            effective_confidence=effective_conf,
            edge=edge, bet_size=size,
            sizing_reason=sz_reason,
            score_line=game.score_line,
        ))
        logger.info(
            f"HARVEST [{game.sport}]: {game.leader} @{price:.3f} "
            f"conf={game.confidence:.3f}{' (stale→'+format(effective_conf,'.3f')+')' if stale else ''} "
            f"edge={edge:.3f} ${size:.2f}"
        )

    return signals, diag
