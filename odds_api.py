"""
odds_api.py — The Odds API integration (v18 NEW)

Flag-gated soccer odds source. Activates only when both ODDS_API_KEY is set
AND ODDS_API_ENABLED=true. Provides the-odds-api.com odds for 20+ soccer
leagues that ESPN doesn't embed.

Sharp-book preference order: pinnacle > betfair > bet365 > others.
Returns data in the same shape as espn.GameOdds for drop-in use by edge.py.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Optional, Dict
import aiohttp

from config import (
    ODDS_API_KEY, ODDS_API_ENABLED, ODDS_API_BASE,
    ODDS_API_REGIONS, ODDS_API_SPORT_MAP, ODDS_API_SHARP_BOOKS,
)
from espn import GameOdds, _devig, _american_to_decimal

logger = logging.getLogger("odds_api")
TIMEOUT = aiohttp.ClientTimeout(total=15)


def is_enabled() -> bool:
    return bool(ODDS_API_ENABLED and ODDS_API_KEY)


def _pick_book(books: list) -> Optional[dict]:
    """Sharp book preferred order."""
    by_key = {(b.get("key") or "").lower(): b for b in books or []}
    for pref in ODDS_API_SHARP_BOOKS:
        if pref in by_key:
            return by_key[pref]
    return books[0] if books else None


def _iso_to_ts(s: str) -> Optional[float]:
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return None


async def _fetch_sport(session: aiohttp.ClientSession, sport_key: str, oa_key: str) -> List[GameOdds]:
    url = f"{ODDS_API_BASE}/sports/{oa_key}/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": ODDS_API_REGIONS,
        "markets": "h2h",
        "oddsFormat": "american",
        "dateFormat": "iso",
    }
    try:
        async with session.get(url, params=params, timeout=TIMEOUT) as r:
            if r.status == 401:
                logger.error("Odds API key rejected (401)")
                return []
            if r.status == 429:
                logger.warning("Odds API rate limited")
                return []
            if r.status != 200:
                logger.debug(f"odds_api {oa_key} status={r.status}")
                return []
            data = await r.json()
    except Exception as e:
        logger.debug(f"odds_api {oa_key}: {e}")
        return []

    out: List[GameOdds] = []
    for game in data or []:
        try:
            home = game.get("home_team", "")
            away = game.get("away_team", "")
            commence = game.get("commence_time", "")
            if not home or not away or not commence:
                continue

            book = _pick_book(game.get("bookmakers") or [])
            if not book:
                continue

            provider = book.get("title") or book.get("key") or ""
            h2h = next(
                (m for m in (book.get("markets") or []) if m.get("key") == "h2h"),
                None,
            )
            if not h2h:
                continue

            home_ml = away_ml = None
            for o in h2h.get("outcomes") or []:
                name = o.get("name", "")
                price = o.get("price")
                if name == home:
                    home_ml = price
                elif name == away:
                    away_ml = price
            if home_ml is None or away_ml is None:
                continue

            hp, ap = _devig(int(home_ml), int(away_ml))
            if hp == 0:
                continue

            out.append(GameOdds(
                espn_id=f"oa_{game.get('id','')}", sport=sport_key,
                home_team=home, away_team=away,
                home_abbrev="", away_abbrev="",
                provider=provider, home_ml=int(home_ml), away_ml=int(away_ml),
                home_prob=hp, away_prob=ap, spread=0.0,
                status="pre", commence_time=commence,
            ))
        except Exception as e:
            logger.debug(f"odds_api normalize: {e}")
    return out


async def fetch_all_soccer(session: aiohttp.ClientSession) -> List[GameOdds]:
    """Fetch odds for every configured soccer league."""
    if not is_enabled():
        return []
    tasks = [
        _fetch_sport(session, sport_key, oa_key)
        for sport_key, oa_key in ODDS_API_SPORT_MAP.items()
    ]
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=30,
        )
    except asyncio.TimeoutError:
        return []
    out = []
    for r in results:
        if isinstance(r, list):
            out.extend(r)
    logger.info(f"odds_api: {len(out)} soccer odds fetched")
    return out
