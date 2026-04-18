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
    # Retry up to 3 times on 429 with exponential backoff
    for attempt in range(3):
        try:
            async with session.get(url, params=params, timeout=TIMEOUT) as r:
                _record_request()
                if r.status == 401:
                    logger.error("odds_api: 401 unauthorized — check ODDS_API_KEY")
                    return None
                if r.status == 429:
                    wait = 2 ** attempt  # 1s, 2s, 4s
                    logger.warning(f"odds_api: 429 rate limited, backing off {wait}s (attempt {attempt+1}/3)")
                    await asyncio.sleep(wait)
                    continue
                if r.status != 200:
                    logger.debug(f"odds_api {path}: HTTP {r.status}")
                    return None
                return await r.json()
        except asyncio.TimeoutError:
            logger.debug(f"odds_api {path}: timeout (attempt {attempt+1})")
            await asyncio.sleep(1)
        except Exception as e:
            logger.debug(f"odds_api {path}: {e}")
            return None
    return None


async def _fetch_events_for_league(
    session: aiohttp.ClientSession, league_slug: str, bookmaker: str
) -> List[dict]:
    """
    Fetch UPCOMING events for a league using its slug.
    `status=pending` excludes already-settled games.
    `bookmaker=X` restricts to events where our bookmaker has odds.
    """
    params = {
        "sport": "football",
        "league": league_slug,
        "status": "pending",
        "bookmaker": bookmaker,
        "limit": 100,
    }
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
# Per-league diagnostic state (exposed to dashboard)
_LEAGUE_DIAG: dict = {}


def get_league_diag() -> dict:
    """Returns snapshot of per-league fetch results for dashboard."""
    return dict(_LEAGUE_DIAG)


async def _fetch_league_cached(
    session: aiohttp.ClientSession,
    sport_key: str,
    league_slug: str,
    bookmakers: List[str],
) -> List[GameOdds]:
    """Per-league TTL-cached fetch. Uses slug + status=pending to get upcoming games."""
    now = time.time()
    cached = _ODDS_CACHE.get(sport_key)
    if cached and (now - cached[0]) < CACHE_TTL_SEC:
        logger.info(f"odds_api[{sport_key}]: cache hit, {len(cached[1])} odds")
        _LEAGUE_DIAG[sport_key] = {
            "cache_hit": True, "events_raw": 0, "events_upcoming": 0,
            "odds_fetched": 0, "final": len(cached[1]), "error": "",
            "slug": league_slug,
        }
        return cached[1]

    diag = {
        "cache_hit": False, "events_raw": 0, "events_upcoming": 0,
        "odds_fetched": 0, "normalized": 0, "final": 0,
        "rejections": {}, "error": "",
        "slug": league_slug,
    }

    # Use first bookmaker for the event filter (the /events endpoint takes 1 bookmaker)
    primary_book = bookmakers[0] if bookmakers else "Bet365"
    events = await _fetch_events_for_league(session, league_slug, primary_book)
    diag["events_raw"] = len(events)
    logger.info(f"odds_api[{sport_key}]: /events returned {len(events)} raw events (slug={league_slug})")
    if not events:
        diag["error"] = "no_events_from_api"
        _LEAGUE_DIAG[sport_key] = diag
        if cached:
            return cached[1]
        _ODDS_CACHE[sport_key] = (now, [])
        return []

    # Only look at events in next 48h (status=pending + further cutoff)
    cutoff = time.time() + 48 * 3600
    upcoming = []
    for ev in events:
        try:
            d = ev.get("date") or ev.get("startsAt") or ""
            if not d:
                continue
            ts = datetime.fromisoformat(d.replace("Z", "+00:00")).timestamp()
            if ts > time.time() and ts < cutoff:
                upcoming.append(ev)
        except Exception as ex:
            logger.debug(f"odds_api[{sport_key}]: date parse err: {ex}")
    diag["events_upcoming"] = len(upcoming)
    logger.info(f"odds_api[{sport_key}]: {len(upcoming)} events within 48h window")
    if not upcoming:
        diag["error"] = "no_events_in_48h_window"
        _LEAGUE_DIAG[sport_key] = diag
        _ODDS_CACHE[sport_key] = (now, [])
        return []

    # Fetch odds in batches of 10 (1 request per 10 events)
    all_game_odds: List[GameOdds] = []
    total_rejections = {"no_home_away": 0, "no_bookmakers": 0, "no_matching_book": 0, "no_ml": 0, "bad_probs": 0}
    total_odds_batch = 0
    for i in range(0, len(upcoming), 10):
        chunk = upcoming[i : i + 10]
        event_ids = [e.get("id") for e in chunk if e.get("id") is not None]
        odds_batch = await _fetch_odds_batch(session, event_ids, bookmakers)
        total_odds_batch += len(odds_batch)
        logger.info(f"odds_api[{sport_key}]: /odds/multi for {len(event_ids)} IDs returned {len(odds_batch)} events with odds")
        for ev in odds_batch:
            if not (ev.get("home") and ev.get("away")):
                total_rejections["no_home_away"] += 1
                continue
            bms = ev.get("bookmakers") or {}
            if not bms:
                total_rejections["no_bookmakers"] += 1
                continue
            book_keys_lower = {k.lower(): k for k in bms.keys()}
            found_book = None
            for book_pref in bookmakers:
                if book_pref.lower() in book_keys_lower:
                    found_book = book_keys_lower[book_pref.lower()]
                    break
            if not found_book:
                total_rejections["no_matching_book"] += 1
                logger.info(f"  event {ev.get('id')}: bookmakers present = {list(bms.keys())} (wanted {bookmakers})")
                continue
            ml = _extract_ml_odds(bms[found_book])
            if not ml:
                total_rejections["no_ml"] += 1
                continue
            go = _normalize_event(ev, sport_key, bookmakers)
            if go:
                all_game_odds.append(go)
            else:
                total_rejections["bad_probs"] += 1

    diag["odds_fetched"] = total_odds_batch
    diag["normalized"] = len(all_game_odds)
    diag["final"] = len(all_game_odds)
    diag["rejections"] = total_rejections
    if not all_game_odds and total_odds_batch > 0:
        diag["error"] = "all_events_rejected"
    elif not all_game_odds:
        diag["error"] = "odds_multi_returned_empty"
    _LEAGUE_DIAG[sport_key] = diag

    if all_game_odds:
        _ODDS_CACHE[sport_key] = (now, all_game_odds)
    logger.info(f"odds_api[{sport_key}]: final {len(all_game_odds)} GameOdds")
    return all_game_odds


# ─── Public API ────────────────────────────────────────────────────────────
# Limit concurrent API calls to avoid 429s (odds-api.io allows 100/hour
# but trips rate limiter with bursts >6 concurrent).
_CONCURRENCY = asyncio.Semaphore(3)


async def _fetch_with_throttle(
    session: aiohttp.ClientSession,
    sport_key: str,
    league_slug: str,
    bookmakers: List[str],
) -> List[GameOdds]:
    async with _CONCURRENCY:
        result = await _fetch_league_cached(session, sport_key, league_slug, bookmakers)
        # Gentle spacing between league fetches
        await asyncio.sleep(0.3)
        return result


async def fetch_all_soccer(session: aiohttp.ClientSession) -> List[GameOdds]:
    if not is_enabled():
        return []
    bookmakers = ODDS_API_BOOKMAKERS or ["Bet365", "DraftKings"]
    tasks = [
        _fetch_with_throttle(session, sport_key, league_slug, bookmakers)
        for sport_key, league_slug in ODDS_API_LEAGUE_MAP.items()
    ]
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=180,
        )
    except asyncio.TimeoutError:
        logger.warning("odds_api: fetch_all_soccer timed out")
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
