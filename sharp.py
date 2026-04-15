"""
sharp.py — Engine 2: Sharp Edge Scanner
Compares Polymarket odds against de-vigged Pinnacle to find mispricings.
Fires PRE-GAME and EARLY-GAME signals.

Quota management: Rotates through 1 sport per scan to stay under
500 requests/month free tier.

Exports:
    SharpSignal  — dataclass for a trade signal
    scan_sharp(clob, positions) → list[SharpSignal]
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional

from odds import fetch_pinnacle_odds, PinnacleOdds, get_remaining_quota, get_calls_today
from clob import ClobInterface, parse_market_tokens
from espn import team_search_terms
from positions import PositionManager
from config import (
    SHARP_MIN_EDGE, SHARP_MAX_PRICE, SHARP_MIN_PRICE,
    SHARP_MIN_PINNACLE_PROB, ODDS_SPORT_KEYS, ODDS_API_KEY,
    KELLY_FRACTION, MAX_POSITION_PCT, MIN_TRADE_SIZE,
    SHARP_MAX_CALLS_PER_DAY, SOCCER_SPORTS,
)

logger = logging.getLogger("sharp")

# All sports the Sharp engine can scan (ordered by typical Polymarket volume)
SHARP_SPORTS = ["nba", "nfl", "mlb", "nhl", "epl", "liga", "ncaab", "ucl", "seriea", "bundes"]

# Rotation index persists across scans
_rotation_idx = 0


@dataclass
class SharpSignal:
    """A trade signal from the Sharp Edge engine."""
    engine: str = "sharp"
    sport: str = ""
    condition_id: str = ""
    market_question: str = ""
    team: str = ""
    side: str = ""
    token_id: str = ""
    clob_price: float = 0.0
    pinnacle_prob: float = 0.0
    edge: float = 0.0
    pinnacle_odds: float = 0.0
    overround: float = 0.0
    kelly_size: float = 0.0
    commence_time: str = ""


def _match_pinnacle_to_polymarket(pin_team: str, outcomes: list[str]) -> Optional[int]:
    """
    Find which Polymarket outcome index matches a Pinnacle team name.
    Uses team_search_terms for fuzzy matching.
    Returns the outcome index or None.
    """
    # Generate search terms from the Pinnacle team name
    # Pinnacle names like "Los Angeles Lakers" — treat full name as team, abbreviation unknown
    pin_lower = pin_team.lower().strip()
    pin_parts = pin_lower.split()
    search_terms = [pin_lower]
    if len(pin_parts) > 1:
        search_terms.append(pin_parts[-1])  # mascot

    for i, outcome in enumerate(outcomes):
        outcome_lower = outcome.lower()
        for term in search_terms:
            if len(term) > 2 and term in outcome_lower:
                return i
    return None


def _compute_kelly_size(price: float, win_prob: float, equity: float) -> float:
    """Quarter-Kelly with proper compounding. No fixed dollar cap."""
    if price <= 0 or price >= 1 or win_prob <= 0 or equity <= 0:
        return 0.0

    b = (1.0 - price) / price
    q = 1.0 - win_prob
    kelly = (win_prob * b - q) / b

    if kelly <= 0:
        return 0.0

    fraction = kelly * KELLY_FRACTION
    fraction = min(fraction, MAX_POSITION_PCT)

    size_dollars = fraction * equity

    if size_dollars < MIN_TRADE_SIZE:
        if equity >= MIN_TRADE_SIZE * 2:
            size_dollars = MIN_TRADE_SIZE
        else:
            return 0.0

    return round(size_dollars, 2)


async def scan_sharp(clob: ClobInterface, positions: PositionManager) -> list[SharpSignal]:
    """
    Sharp Edge scan. Rotates 1 sport per call to conserve API quota.
    Returns list of actionable signals.
    """
    global _rotation_idx
    signals = []

    if not ODDS_API_KEY:
        logger.debug("No ODDS_API_KEY — Sharp Edge disabled")
        return signals

    # Quota checks
    quota = get_remaining_quota()
    if quota is not None and quota <= 10:
        logger.warning(f"Odds API quota critically low ({quota}) — skipping")
        return signals

    if get_calls_today() >= SHARP_MAX_CALLS_PER_DAY:
        logger.info(f"Daily call limit reached ({SHARP_MAX_CALLS_PER_DAY}) — skipping")
        return signals

    # Pick 1 sport (rotation)
    sport = SHARP_SPORTS[_rotation_idx % len(SHARP_SPORTS)]
    _rotation_idx += 1

    logger.info(f"Sharp scan: {sport} (rotation {_rotation_idx})")

    # Step 1: Fetch Pinnacle odds for this sport
    pinnacle_games = await fetch_pinnacle_odds(sport)
    if not pinnacle_games:
        return signals

    # Step 2: Fetch Polymarket events
    poly_events = await clob.fetch_polymarket_events(sport)
    if not poly_events:
        return signals

    # Step 3: Match and compare
    for pin_game in pinnacle_games:
        for poly_event in poly_events:
            parsed = parse_market_tokens(poly_event)
            if not parsed:
                continue

            if positions.has_position_for(parsed["condition_id"]):
                continue

            outcomes = parsed["outcomes"]

            # Match both Pinnacle teams to Polymarket outcomes by name
            home_idx = _match_pinnacle_to_polymarket(pin_game.home_team, outcomes)
            away_idx = _match_pinnacle_to_polymarket(pin_game.away_team, outcomes)

            if home_idx is None or away_idx is None:
                continue

            # Prevent both matching same outcome
            if home_idx == away_idx:
                continue

            # Check both sides for mispricing
            for team_name, pin_prob, pin_odds, matched_idx in [
                (pin_game.home_team, pin_game.home_prob, pin_game.home_decimal_odds, home_idx),
                (pin_game.away_team, pin_game.away_prob, pin_game.away_decimal_odds, away_idx),
            ]:
                if pin_prob < SHARP_MIN_PINNACLE_PROB:
                    continue

                # For 3-way soccer: if draw prob is high, the win probs are lower
                # meaning we need bigger edge to justify the bet
                # The de-vigged prob already accounts for this correctly

                token_id = parsed["token_ids"][matched_idx]
                clob_price = clob.get_price(token_id, "BUY")

                if clob_price is None:
                    continue

                if clob_price > SHARP_MAX_PRICE or clob_price < SHARP_MIN_PRICE:
                    continue

                edge = pin_prob - clob_price
                if edge < SHARP_MIN_EDGE:
                    continue

                kelly_size = _compute_kelly_size(clob_price, pin_prob, positions.equity)
                if kelly_size <= 0:
                    continue

                # Determine side based on which outcome this team IS
                # outcomes[matched_idx] is the team name, the token at that index
                # is what we buy. The "side" label for the position.
                side = outcomes[matched_idx]

                signal = SharpSignal(
                    sport=sport,
                    condition_id=parsed["condition_id"],
                    market_question=parsed["question"],
                    team=team_name,
                    side=side,
                    token_id=token_id,
                    clob_price=clob_price,
                    pinnacle_prob=pin_prob,
                    edge=edge,
                    pinnacle_odds=pin_odds,
                    overround=pin_game.overround,
                    kelly_size=kelly_size,
                    commence_time=pin_game.commence_time,
                )
                signals.append(signal)
                logger.info(
                    f"⚡ SHARP: {team_name} @{clob_price:.3f} | "
                    f"pinnacle={pin_prob:.3f} edge={edge:.3f} | ${kelly_size:.2f}"
                )

            # Only match each Pinnacle game to one Polymarket event
            break

    logger.info(f"Sharp {sport}: {len(signals)} signals")
    return signals
