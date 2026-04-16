"""
harvest.py — Engine 1: Blowout Harvest
ESPN verified blowouts → Polymarket moneyline. Hold to resolution.
Now uses sport-specific Kelly sizing (soccer=full, NBA=half).
"""

import logging
from dataclasses import dataclass
from typing import Optional
from espn import fetch_verified_games, VerifiedGame
from teams import generate_search_terms as team_search_terms, find_team_in_outcomes, find_team_in_text
from clob import ClobInterface, parse_market_tokens
from positions import PositionManager
from sizing import compute_bet_size
from config import HARVEST_MIN_CONFIDENCE, HARVEST_MAX_PRICE, HARVEST_MIN_PRICE, HARVEST_MIN_EDGE, MIN_MARKET_LIQUIDITY

logger = logging.getLogger("harvest")


@dataclass
class HarvestSignal:
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
    bet_size: float = 0.0
    score_line: str = ""


def _match(game, market):
    parsed = parse_market_tokens(market)
    if not parsed or parsed.get("liquidity", 0) < MIN_MARKET_LIQUIDITY:
        return None
    q = parsed["question"]
    outcomes = parsed["outcomes"]

    # Both teams must be found (prevents matching futures/wrong games)
    leader_found = find_team_in_text(game.leader, game.leader_abbrev, q) or \
                   find_team_in_outcomes(game.leader, game.leader_abbrev, outcomes) >= 0
    trailer_found = find_team_in_text(game.trailer, game.trailer_abbrev, q) or \
                    find_team_in_outcomes(game.trailer, game.trailer_abbrev, outcomes) >= 0

    if not (leader_found and trailer_found):
        return None

    # Find which outcome is the leader
    idx = find_team_in_outcomes(game.leader, game.leader_abbrev, outcomes)
    parsed["leader_idx"] = idx if idx >= 0 else 0
    return parsed


async def scan_harvest(clob, positions):
    signals = []
    blowout_log = []  # diagnostic: every blowout and its fate
    blowouts, live_games = await fetch_verified_games()
    if not blowouts:
        return signals, live_games, blowout_log
    cache = {}
    for game in blowouts:
        entry = {
            "sport": game.sport, "leader": game.leader_abbrev,
            "trailer": game.trailer_abbrev, "lead": game.lead,
            "confidence": game.confidence, "score_line": game.score_line,
            "status": "pending", "reason": "",
        }

        if game.confidence < HARVEST_MIN_CONFIDENCE:
            entry.update(status="skip", reason=f"conf {game.confidence:.3f} < {HARVEST_MIN_CONFIDENCE}")
            blowout_log.append(entry)
            continue

        if game.sport not in cache:
            cache[game.sport] = await clob.fetch_polymarket_events(game.sport)

        matched = None
        for ev in cache[game.sport]:
            r = _match(game, ev)
            if r:
                matched = r
                break

        if not matched:
            entry.update(status="skip", reason="no Polymarket market found")
            blowout_log.append(entry)
            logger.info(f"⚠️ BLOWOUT {game.leader_abbrev} +{game.lead} but NO POLY MARKET: {game.score_line}")
            continue

        if positions.has_position_for(matched["condition_id"]):
            entry.update(status="skip", reason="already positioned")
            blowout_log.append(entry)
            continue

        tid = matched["token_ids"][matched["leader_idx"]]
        price = clob.get_price(tid, "BUY")

        if price is None:
            entry.update(status="skip", reason="price unavailable")
            blowout_log.append(entry)
            continue
        if price > HARVEST_MAX_PRICE:
            entry.update(status="skip", reason=f"price {price:.3f} > max {HARVEST_MAX_PRICE}", price=price)
            blowout_log.append(entry)
            logger.info(f"⚠️ BLOWOUT {game.leader_abbrev} +{game.lead} price={price:.3f} TOO HIGH (>{HARVEST_MAX_PRICE})")
            continue
        if price < HARVEST_MIN_PRICE:
            entry.update(status="skip", reason=f"price {price:.3f} < min {HARVEST_MIN_PRICE}", price=price)
            blowout_log.append(entry)
            continue

        edge = game.confidence - price
        if edge < HARVEST_MIN_EDGE:
            entry.update(status="skip", reason=f"edge {edge:.3f} < min {HARVEST_MIN_EDGE}", price=price)
            blowout_log.append(entry)
            continue

        bet = compute_bet_size("harvest", price, game.confidence, positions.equity, positions, sport=game.sport)
        if bet <= 0:
            entry.update(status="skip", reason="bet size zero (capital limit?)", price=price)
            blowout_log.append(entry)
            continue

        entry.update(status="signal", reason="TRADING", price=price, edge=edge, bet=bet)
        blowout_log.append(entry)

        signals.append(HarvestSignal(
            sport=game.sport, game=game, condition_id=matched["condition_id"],
            market_question=matched["question"], team=game.leader, side="YES",
            token_id=tid, clob_price=price, confidence=game.confidence,
            edge=edge, bet_size=bet, score_line=game.score_line,
        ))
        logger.info(f"🎯 HARVEST [{game.sport}]: {game.leader} YES@{price:.3f} conf={game.confidence:.3f} edge={edge:.3f} ${bet}")

    return signals, live_games, blowout_log
