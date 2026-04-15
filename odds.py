"""
odds.py — The Odds API Integration (Sharp Edge Engine)
Fetches Pinnacle odds, removes vig to get true probabilities.
Handles both 2-way (US sports) and 3-way (soccer) markets.

Exports:
    PinnacleOdds  — dataclass with de-vigged probabilities
    fetch_pinnacle_odds(sport) → list[PinnacleOdds]
    get_remaining_quota() → int
"""

import logging
from dataclasses import dataclass, field
from typing import Optional
import aiohttp

from config import ODDS_API_KEY, ODDS_API_BASE, ODDS_SPORT_KEYS, SOCCER_SPORTS

logger = logging.getLogger("odds")

_remaining_quota: Optional[int] = None
_calls_today: int = 0


@dataclass
class PinnacleOdds:
    """De-vigged Pinnacle odds for a single game."""
    sport: str
    home_team: str
    away_team: str
    commence_time: str
    home_prob: float
    away_prob: float
    draw_prob: float = 0.0        # >0 for soccer
    home_decimal_odds: float = 0.0
    away_decimal_odds: float = 0.0
    draw_decimal_odds: float = 0.0
    overround: float = 0.0
    is_three_way: bool = False


def _devig_two_way(odds_a: float, odds_b: float) -> tuple[float, float, float]:
    """De-vig 2-way market. Returns (prob_a, prob_b, overround)."""
    if odds_a <= 1.0 or odds_b <= 1.0:
        return 0.0, 0.0, 0.0
    imp_a = 1.0 / odds_a
    imp_b = 1.0 / odds_b
    overround = imp_a + imp_b
    if overround <= 0:
        return 0.0, 0.0, 0.0
    return round(imp_a / overround, 4), round(imp_b / overround, 4), round(overround, 4)


def _devig_three_way(odds_h: float, odds_d: float, odds_a: float) -> tuple[float, float, float, float]:
    """De-vig 3-way market (soccer). Returns (home, draw, away, overround)."""
    if odds_h <= 1.0 or odds_d <= 1.0 or odds_a <= 1.0:
        return 0.0, 0.0, 0.0, 0.0
    imp_h = 1.0 / odds_h
    imp_d = 1.0 / odds_d
    imp_a = 1.0 / odds_a
    overround = imp_h + imp_d + imp_a
    if overround <= 0:
        return 0.0, 0.0, 0.0, 0.0
    return (
        round(imp_h / overround, 4),
        round(imp_d / overround, 4),
        round(imp_a / overround, 4),
        round(overround, 4),
    )


def get_remaining_quota() -> Optional[int]:
    return _remaining_quota


def get_calls_today() -> int:
    return _calls_today


def reset_daily_calls():
    global _calls_today
    _calls_today = 0


async def fetch_pinnacle_odds(sport: str) -> list[PinnacleOdds]:
    """Fetch and de-vig Pinnacle odds for a sport."""
    global _remaining_quota, _calls_today

    sport_key = ODDS_SPORT_KEYS.get(sport)
    if not sport_key:
        return []

    if not ODDS_API_KEY:
        return []

    if _remaining_quota is not None and _remaining_quota <= 10:
        logger.warning(f"Odds API quota low: {_remaining_quota} — skipping")
        return []

    is_soccer = sport in SOCCER_SPORTS
    url = f"{ODDS_API_BASE}/{sport_key}/odds/"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "us,eu",
        "markets": "h2h",
        "bookmakers": "pinnacle",
    }

    results = []

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                remaining = resp.headers.get("x-requests-remaining")
                if remaining is not None:
                    try:
                        _remaining_quota = int(remaining)
                    except ValueError:
                        pass

                _calls_today += 1

                if resp.status == 401:
                    logger.error("Odds API: invalid API key")
                    return []
                if resp.status == 429:
                    logger.error("Odds API: rate limited")
                    _remaining_quota = 0
                    return []
                if resp.status != 200:
                    logger.warning(f"Odds API {sport} returned {resp.status}")
                    return []

                data = await resp.json()

    except Exception as e:
        logger.error(f"Odds API fetch failed for {sport}: {e}")
        return []

    for event in data:
        try:
            home_team = event.get("home_team", "")
            away_team = event.get("away_team", "")
            commence = event.get("commence_time", "")

            bookmakers = event.get("bookmakers", [])
            pinnacle = None
            for bm in bookmakers:
                if bm.get("key") == "pinnacle":
                    pinnacle = bm
                    break
            if not pinnacle:
                continue

            markets = pinnacle.get("markets", [])
            h2h_market = None
            for m in markets:
                if m.get("key") == "h2h":
                    h2h_market = m
                    break
            if not h2h_market:
                continue

            outcomes = h2h_market.get("outcomes", [])

            # Parse outcomes into home/away/draw
            home_decimal = away_decimal = draw_decimal = 0.0
            for outcome in outcomes:
                name = outcome.get("name", "")
                price = float(outcome.get("price", 0))
                if name == home_team:
                    home_decimal = price
                elif name == away_team:
                    away_decimal = price
                elif name.lower() == "draw":
                    draw_decimal = price

            if home_decimal <= 1.0 or away_decimal <= 1.0:
                continue

            # De-vig based on market type
            if is_soccer and draw_decimal > 1.0:
                home_prob, draw_prob, away_prob, overround = _devig_three_way(
                    home_decimal, draw_decimal, away_decimal
                )
                three_way = True
            else:
                home_prob, away_prob, overround = _devig_two_way(home_decimal, away_decimal)
                draw_prob = 0.0
                three_way = False

            if home_prob == 0.0:
                continue

            results.append(PinnacleOdds(
                sport=sport,
                home_team=home_team,
                away_team=away_team,
                commence_time=commence,
                home_prob=home_prob,
                away_prob=away_prob,
                draw_prob=draw_prob,
                home_decimal_odds=home_decimal,
                away_decimal_odds=away_decimal,
                draw_decimal_odds=draw_decimal,
                overround=overround,
                is_three_way=three_way,
            ))

        except Exception as e:
            logger.error(f"Error parsing Odds API event in {sport}: {e}")
            continue

    logger.info(f"Odds API {sport}: {len(results)} games (quota: {_remaining_quota})")
    return results
