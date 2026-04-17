"""
odds_api.py — odds-api.io integration (v18.3)

Integrates with odds-api.io (NOT the-odds-api.com — different service).
Free tier: 100 requests/hour, 2 bookmakers of user's choice.

Why this is better than the old integration:
  - /odds/multi endpoint fetches up to 10 events in 1 request
  - 100/hour budget easily covers 4-8 leagues with 30min refresh
  - Response is one-click away from the GameOdds shape edge.py needs

API flow:
  1. GET /events?apiKey=X&sport=football&league=<n>&limit=50
     → list of upcoming fixture IDs
  2. GET /odds/multi?apiKey=X&eventIds=1,2,...&bookmakers=Bet365,DraftKings
     → odds for all events in a single request
  3. Normalize each event to GameOdds (uses de-vigged ML odds)

Auth: ?apiKey=<key> query param (not header).
Env: ODDS_API_KEY, ODDS_API_ENABLED, ODDS_API_BOOKMAKERS, ODDS_API_LEAGUES.
"""
import asyncio
import logging
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple
import aiohttp

from config import (
    ODDS_API_KEY, ODDS_API_ENABLED, ODDS_API_BASE,
    ODDS_API_BOOKMAKERS, ODDS_API_LEAGUE_MAP,
)
from espn import GameOdds

logger = logging.getLogger("odds_api")
TIMEOUT = aiohttp.ClientTimeout(total=15)

# ─── TTL cache ─────────────────────────────────────────────────────────────
_ODDS_CACHE: Dict[str, tuple] = {}
CACHE_TTL_SEC = 1800  # 30 minutes

# Per-hour rate limit tracking
_REQUEST_TIMESTAMPS: List[float] = []
HOURLY_CAP = 95  # leave headroom of 5 from 100/hour free tier


def is_enabled() -> bool:
    return bool(ODDS_API_ENABLED and ODDS_API_KEY)


def _under_rate_limit() -> bool:
    global _REQUEST_TIMESTAMPS
    now = time.time()
    _REQUEST_TIMESTAMPS = [t for t in _REQUEST_TIMESTAMPS if now - t < 3600]
    return len(_REQUEST_TIMESTAMPS) < HOURLY_CAP


def _record_request():
    _REQUEST_TIMESTAMPS.append(time.time())


def hourly_budget() -> dict:
    now = time.time()
    recent = [t for t in _REQUEST_TIMESTAMPS if now - t < 3600]
    return {"used": len(recent), "limit": 100, "remaining": max(0, 100 - len(recent))}


# ─── Helpers ───────────────────────────────────────────────────────────────
def _decimal_to_american(d: float) -> int:
    if d <= 1.0:
        return 0
    if d >= 2.0:
        return int(round((d - 1) * 100))
    return int(round(-100 / (d - 1)))


def _decimal_devig_3way(h: float, d: float, a: float) -> Tuple[float, float, float]:
    if h <= 1 or a <= 1:
        return 0, 0, 0
    hp, ap = 1 / h, 1 / a
    dp = 1 / d if d > 1 else 0
    t = hp + ap + dp
    if t <= 0:
        return 0, 0, 0
    return round(hp / t, 4), round(dp / t, 4), round(ap / t, 4)


def _decimal_devig_2way(h: float, a: float) -> Tuple[float, float]:
    if h <= 1 or a <= 1:
        return 0, 0
    hp, ap = 1 / h, 1 / a
    t = hp + ap
    return round(hp / t, 4), round(ap / t, 4)


# ─── HTTP ──────────────────────────────────────────────────────────────────
async def _get(session: aiohttp.ClientSession, path: str, params: dict) -> Optional[dict]:
    if not _under_rate_limit():
        logger.warning("odds_api: hourly rate limit reached, skipping")
        return None
    params = {**params, "apiKey": ODDS_API_KEY}
    url = f"{ODDS_API_BASE}{path}"
    try:
        async with session.get(url, params=params, timeout=TIMEOUT) as r:
            _record_request()
            if r.status == 401:
                logger.error("odds_api: 401 unauthorized — check ODDS_API_KEY")
                return None
            if r.status == 429:
                logger.warning("odds_api: 429 rate limited")
                return None
            if r.status != 200:
                logger.debug(f"odds_api {path}: HTTP {r.status}")
                return None
            return await r.json()
    except asyncio.TimeoutError:
        logger.debug(f"odds_api {path}: timeout")
        return None
    except Exception as e:
        logger.debug(f"odds_api {path}: {e}")
        return None


async def _fetch_events_for_league(
    session: aiohttp.ClientSession, sport_api_name: str, league_api_name: str
) -> List[dict]:
    """
    League name filtering: odds-api.io may or may not accept 'league' param,
    and the exact name convention isn't documented. We pass it when provided
    and gracefully degrade if the server ignores it.
    """
    params = {"sport": sport_api_name, "limit": 100}
    if league_api_name:
        params["league"] = league_api_name
    data = await _get(session, "/events", params)
    if not data:
        return []
    return data if isinstance(data, list) else data.get("events", []) or []


async def _fetch_all_football_events(
    session: aiohttp.ClientSession
) -> List[dict]:
    """
    Fetch ALL football events from odds-api.io (single request, unfiltered).
    We then filter client-side by league name keyword matching.
    """
    params = {"sport": "football", "limit": 200}
    data = await _get(session, "/events", params)
    if not data:
        return []
    return data if isinstance(data, list) else data.get("events", []) or []


async def _fetch_odds_batch(
    session: aiohttp.ClientSession, event_ids: List, bookmakers: List[str]
) -> List[dict]:
    if not event_ids:
        return []
    params = {
        "eventIds": ",".join(str(e) for e in event_ids[:10]),
        "bookmakers": ",".join(bookmakers),
    }
    data = await _get(session, "/odds/multi", params)
    if not data:
        return []
    return data if isinstance(data, list) else data.get("events", []) or []


# ─── Normalization ─────────────────────────────────────────────────────────
def _extract_ml_odds(bookmaker_markets) -> Optional[Tuple[float, float, Optional[float]]]:
    if not isinstance(bookmaker_markets, list):
        return None
    for market in bookmaker_markets:
        if not isinstance(market, dict):
            continue
        if market.get("name") != "ML":
            continue
        odds_list = market.get("odds") or []
        if not odds_list:
            continue
        o = odds_list[0]
        try:
            h = float(o.get("home"))
            a = float(o.get("away"))
            d_raw = o.get("draw")
            d = float(d_raw) if d_raw not in (None, "") else None
            return (h, a, d)
        except (TypeError, ValueError):
            continue
    return None


def _normalize_event(event: dict, sport_key: str, preferred_books: List[str]) -> Optional[GameOdds]:
    try:
        eid = event.get("id")
        home = event.get("home") or ""
        away = event.get("away") or ""
        date_str = event.get("date") or event.get("startsAt") or ""
        bookmakers = event.get("bookmakers") or {}
        if not home or not away or not bookmakers:
            return None

        picked = None
        picked_name = ""
        # Case-insensitive matching of user's preferred bookmakers
        book_keys_lower = {k.lower(): k for k in bookmakers.keys()}
        for book_pref in preferred_books:
            actual_key = book_keys_lower.get(book_pref.lower())
            if actual_key and bookmakers.get(actual_key):
                extracted = _extract_ml_odds(bookmakers[actual_key])
                if extracted:
                    picked = extracted
                    picked_name = actual_key
                    break

        if not picked:
            return None

        h_dec, a_dec, d_dec = picked
        if d_dec is not None and d_dec > 1:
            hp, dp, ap = _decimal_devig_3way(h_dec, d_dec, a_dec)
            if hp + ap == 0:
                return None
            total_non_draw = hp + ap
            home_prob = round(hp + dp * (hp / total_non_draw), 4)
            away_prob = round(ap + dp * (ap / total_non_draw), 4)
        else:
            home_prob, away_prob = _decimal_devig_2way(h_dec, a_dec)

        if home_prob <= 0 or away_prob <= 0:
            return None

        return GameOdds(
            espn_id=f"oa_{eid}",
            sport=sport_key,
            home_team=home,
            away_team=away,
            home_abbrev="",
            away_abbrev="",
            provider=picked_name,
            home_ml=_decimal_to_american(h_dec),
            away_ml=_decimal_to_american(a_dec),
            home_prob=home_prob,
            away_prob=away_prob,
            spread=0.0,
            status="pre",
            commence_time=date_str,
        )
    except Exception as e:
        logger.debug(f"normalize error: {e}")
        return None


# ─── League fetch with caching ─────────────────────────────────────────────
# Global football events cache (shared across all league filters)
_EVENTS_CACHE: Dict[str, tuple] = {}


async def _get_all_football_events_cached(session: aiohttp.ClientSession) -> List[dict]:
    now = time.time()
    cached = _EVENTS_CACHE.get("football")
    if cached and (now - cached[0]) < CACHE_TTL_SEC:
        return cached[1]
    events = await _fetch_all_football_events(session)
    if events:
        _EVENTS_CACHE["football"] = (now, events)
    elif cached:
        return cached[1]
    return events


def _event_matches_league(event: dict, match_keywords: List[str]) -> bool:
    """Check if the event's league field matches any keyword."""
    league = (event.get("league") or event.get("leagueName") or "").lower()
    if not league:
        return False
    return any(kw.lower() in league for kw in match_keywords)


async def _fetch_league_cached(
    session: aiohttp.ClientSession,
    sport_key: str,
    match_keywords: List[str],
    bookmakers: List[str],
) -> List[GameOdds]:
    now = time.time()
    cached = _ODDS_CACHE.get(sport_key)
    if cached and (now - cached[0]) < CACHE_TTL_SEC:
        return cached[1]

    # Pull all football events once (shared across league filters)
    all_events = await _get_all_football_events_cached(session)
    if not all_events:
        if cached:
            return cached[1]
        return []

    # Filter to this league + next 48h
    cutoff = time.time() + 48 * 3600
    upcoming = []
    for ev in all_events:
        if not _event_matches_league(ev, match_keywords):
            continue
        try:
            d = ev.get("date") or ev.get("startsAt") or ""
            if not d:
                continue
            ts = datetime.fromisoformat(d.replace("Z", "+00:00")).timestamp()
            if ts > time.time() and ts < cutoff:
                upcoming.append(ev)
        except Exception:
            pass
    if not upcoming:
        _ODDS_CACHE[sport_key] = (now, [])
        return []

    # Fetch odds in batches of 10 (1 request per 10 events)
    all_game_odds: List[GameOdds] = []
    for i in range(0, len(upcoming), 10):
        chunk = upcoming[i : i + 10]
        event_ids = [e.get("id") for e in chunk if e.get("id") is not None]
        odds_batch = await _fetch_odds_batch(session, event_ids, bookmakers)
        for ev in odds_batch:
            go = _normalize_event(ev, sport_key, bookmakers)
            if go:
                all_game_odds.append(go)

    if all_game_odds:
        _ODDS_CACHE[sport_key] = (now, all_game_odds)
    return all_game_odds


# ─── Public API ────────────────────────────────────────────────────────────
async def fetch_all_soccer(session: aiohttp.ClientSession) -> List[GameOdds]:
    if not is_enabled():
        return []
    bookmakers = ODDS_API_BOOKMAKERS or ["Bet365", "DraftKings"]
    tasks = [
        _fetch_league_cached(session, sport_key, match_keywords, bookmakers)
        for sport_key, match_keywords in ODDS_API_LEAGUE_MAP.items()
    ]
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=120,
        )
    except asyncio.TimeoutError:
        return []
    out: List[GameOdds] = []
    for r in results:
        if isinstance(r, list):
            out.extend(r)
    budget = hourly_budget()
    logger.info(
        f"odds_api: {len(out)} soccer odds from {len(ODDS_API_LEAGUE_MAP)} leagues "
        f"(hourly budget {budget['used']}/{budget['limit']})"
    )
    return out
