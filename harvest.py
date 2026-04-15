"""
harvest.py — Engine 1: Blowout Harvest Scanner
Monitors ESPN live scores for verified blowouts, matches them to
Polymarket moneyline markets, checks CLOB prices, returns trade signals.

Exports:
    HarvestSignal  — dataclass for a trade signal
    scan_harvest(clob, positions) → (list[HarvestSignal], list[dict])
        Returns (actionable signals, all live games for dashboard)
"""

import logging
from dataclasses import dataclass
from typing import Optional

from espn import fetch_verified_games, team_search_terms, VerifiedGame
from clob import ClobInterface, parse_market_tokens
from positions import PositionManager
from config import (
    HARVEST_MIN_CONFIDENCE, HARVEST_MAX_PRICE, HARVEST_MIN_PRICE,
    HARVEST_MIN_EDGE, KELLY_FRACTION, MAX_POSITION_PCT,
    MIN_TRADE_SIZE,
)

logger = logging.getLogger("harvest")


@dataclass
class HarvestSignal:
    """A trade signal from the Harvest engine."""
    engine: str = "harvest"
    sport: str = ""
    game: Optional[VerifiedGame] = None
    condition_id: str = ""
    market_question: str = ""
    team: str = ""
    side: str = "YES"
    token_id: str = ""
    clob_price: float = 0.0
    confidence: float = 0.0
    edge: float = 0.0
    kelly_size: float = 0.0
    score_line: str = ""


def _match_team_to_market(game: VerifiedGame, market: dict) -> Optional[dict]:
    """
    Match the leading team from an ESPN game to a Polymarket market.
    Requires BOTH teams to be found (prevents matching futures).
    Returns parsed market tokens with leader_idx, or None.
    """
    parsed = parse_market_tokens(market)
    if not parsed:
        return None

    question = parsed["question"].lower()
    outcomes = parsed["outcomes"]

    # Check leader is in market
    leader_terms = team_search_terms(game.leader, game.leader_abbrev)
    leader_found = False
    for term in leader_terms:
        if term in question:
            leader_found = True
            break
        for outcome in outcomes:
            if term in outcome.lower():
                leader_found = True
                break
        if leader_found:
            break

    if not leader_found:
        return None

    # Check trailer is in market (ensures it's THIS game)
    trailer_terms = team_search_terms(game.trailer, game.trailer_abbrev)
    trailer_found = False
    for term in trailer_terms:
        if term in question:
            trailer_found = True
            break
        for outcome in outcomes:
            if term in outcome.lower():
                trailer_found = True
                break
        if trailer_found:
            break

    if not trailer_found:
        return None

    # Find which outcome index is the leader
    leader_idx = None
    for i, outcome in enumerate(outcomes):
        outcome_lower = outcome.lower()
        for term in leader_terms:
            if term in outcome_lower:
                leader_idx = i
                break
        if leader_idx is not None:
            break

    if leader_idx is None:
        leader_idx = 0  # fallback

    parsed["leader_idx"] = leader_idx
    return parsed


def _compute_kelly_size(price: float, win_prob: float, equity: float) -> float:
    """
    Quarter-Kelly for binary market with proper compounding.

    The bet scales as a PERCENTAGE of equity (capped at MAX_POSITION_PCT).
    No fixed dollar ceiling — that's the whole point of Kelly compounding.
    As equity grows, bets grow proportionally.

    Safety layers:
      1. Quarter-Kelly (25% of optimal = very conservative)
      2. MAX_POSITION_PCT cap (10% of equity per trade)
      3. MIN_TRADE_SIZE floor (skip if equity can't support it)
    """
    if price <= 0 or price >= 1 or win_prob <= 0 or equity <= 0:
        return 0.0

    b = (1.0 - price) / price   # net payout ratio ($1 payout, cost = price)
    q = 1.0 - win_prob
    kelly = (win_prob * b - q) / b

    if kelly <= 0:
        return 0.0  # No edge — don't bet

    fraction = kelly * KELLY_FRACTION          # quarter-Kelly
    fraction = min(fraction, MAX_POSITION_PCT)  # hard cap at 10% of equity

    size_dollars = fraction * equity

    # Floor: skip tiny bets, but don't force a bet larger than Kelly says
    if size_dollars < MIN_TRADE_SIZE:
        if equity >= MIN_TRADE_SIZE * 2:
            # Equity is sufficient — round up to minimum
            size_dollars = MIN_TRADE_SIZE
        else:
            return 0.0  # Equity too low

    return round(size_dollars, 2)


async def scan_harvest(clob: ClobInterface, positions: PositionManager) -> tuple[list[HarvestSignal], list[dict]]:
    """
    Full harvest scan cycle.
    Returns (signals, all_live_games) — signals for execution, games for dashboard.
    """
    signals = []

    # Step 1: Get verified blowouts + all live games
    blowouts, live_games = await fetch_verified_games()

    if not blowouts:
        logger.debug("No verified blowouts found")
        return signals, live_games

    logger.info(f"Found {len(blowouts)} verified blowouts")

    # Step 2: Cache Polymarket events per sport to avoid duplicate fetches
    sport_events_cache: dict[str, list[dict]] = {}

    for game in blowouts:
        if game.confidence < HARVEST_MIN_CONFIDENCE:
            continue

        # Fetch events (cached per sport)
        sport = game.sport
        if sport not in sport_events_cache:
            sport_events_cache[sport] = await clob.fetch_polymarket_events(sport)
        events = sport_events_cache[sport]

        # Match game to market
        matched_market = None
        for event in events:
            result = _match_team_to_market(game, event)
            if result:
                matched_market = result
                break

        if not matched_market:
            logger.debug(f"No Polymarket match for {game.leader} vs {game.trailer}")
            continue

        if positions.has_position_for(matched_market["condition_id"]):
            logger.debug(f"Already positioned on {matched_market['question']}")
            continue

        # Get real CLOB price
        leader_idx = matched_market.get("leader_idx", 0)
        token_id = matched_market["token_ids"][leader_idx]
        clob_price = clob.get_price(token_id, "BUY")

        if clob_price is None:
            logger.warning(f"Could not get CLOB price for {token_id[:16]}")
            continue

        if clob_price > HARVEST_MAX_PRICE:
            logger.debug(f"Price too high ({clob_price:.3f}) for {game.leader}")
            continue
        if clob_price < HARVEST_MIN_PRICE:
            logger.debug(f"Price too low ({clob_price:.3f}) — market disagrees on {game.leader}")
            continue

        edge = game.confidence - clob_price
        if edge < HARVEST_MIN_EDGE:
            logger.debug(f"Edge too thin ({edge:.3f}) for {game.leader}")
            continue

        kelly_size = _compute_kelly_size(clob_price, game.confidence, positions.equity)
        if kelly_size <= 0:
            logger.debug(f"Kelly size zero — equity too low or no edge")
            continue

        signal = HarvestSignal(
            sport=game.sport,
            game=game,
            condition_id=matched_market["condition_id"],
            market_question=matched_market["question"],
            team=game.leader,
            side="YES",
            token_id=token_id,
            clob_price=clob_price,
            confidence=game.confidence,
            edge=edge,
            kelly_size=kelly_size,
            score_line=game.score_line,
        )
        signals.append(signal)
        logger.info(
            f"🎯 HARVEST: {game.leader} YES@{clob_price:.3f} | "
            f"conf={game.confidence:.3f} edge={edge:.3f} | ${kelly_size:.2f}"
        )

    return signals, live_games
