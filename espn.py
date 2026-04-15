"""
espn.py — ESPN Live Score Verification Engine
Battle-tested blowout detection across 17 sports/leagues.
Fetches live scores from ESPN's free API, computes game elapsed %,
and determines blowout confidence levels.

Exports:
    VerifiedGame  — dataclass with all game state
    fetch_verified_games() → list[VerifiedGame] (sorted by confidence desc)
    fetch_all_live_games() → list[dict] (all in-progress games for dashboard)
    team_search_terms(full_name, abbrev) → list[str] for Polymarket matching
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional
import aiohttp

from config import ESPN_SPORTS, WIN_THRESHOLDS, SOCCER_SPORTS

logger = logging.getLogger("espn")

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"


@dataclass
class VerifiedGame:
    """Complete state of a verified in-progress game."""
    espn_id: str
    sport: str
    home_team: str
    away_team: str
    home_abbrev: str
    away_abbrev: str
    home_score: int
    away_score: int
    leader: str
    leader_abbrev: str
    trailer: str
    trailer_abbrev: str
    lead: int
    period: int
    clock: str
    elapsed_pct: float
    confidence: float
    level: str
    score_line: str


def team_search_terms(full_name: str, abbrev: str) -> list[str]:
    """
    Generate search terms for matching a team to Polymarket markets.
    Returns multiple variants to increase match probability.
    """
    terms = []
    full_lower = full_name.lower().strip()
    abbrev_lower = abbrev.lower().strip()

    terms.append(full_lower)

    parts = full_lower.split()
    if len(parts) > 1:
        terms.append(parts[-1])

    terms.append(abbrev_lower)

    if len(parts) == 3:
        initials = parts[0][0] + parts[1][0]
        terms.append(f"{initials} {parts[-1]}")

    city_abbrevs = {
        "new york": "ny", "new jersey": "nj", "los angeles": "la",
        "san francisco": "sf", "san antonio": "sa", "san diego": "sd",
        "golden state": "gs", "oklahoma city": "okc", "tampa bay": "tb",
        "green bay": "gb", "kansas city": "kc", "new orleans": "no",
        "new england": "ne", "st louis": "stl", "st. louis": "stl",
        "inter miami": "inter", "real madrid": "real",
        "manchester city": "man city", "manchester united": "man utd",
    }
    for city, short in city_abbrevs.items():
        if full_lower.startswith(city):
            mascot = full_lower.replace(city, "").strip()
            if mascot:
                terms.append(f"{short} {mascot}")
            else:
                terms.append(short)
            break

    return list(dict.fromkeys(terms))


def _get_threshold_key(sport: str) -> str:
    """Map sport key to WIN_THRESHOLDS key. All soccer leagues share 'soccer'."""
    if sport in SOCCER_SPORTS:
        return "soccer"
    return sport


def _compute_elapsed_pct(sport: str, period: int, clock: str, status_detail: str) -> float:
    """Compute what fraction of the game has elapsed."""
    sport_cfg = ESPN_SPORTS.get(sport, {})
    total_periods = sport_cfg.get("periods", 4)

    detail_lower = status_detail.lower() if status_detail else ""
    if "final" in detail_lower or "end" in detail_lower:
        return 1.0
    if "halftime" in detail_lower:
        return 0.5

    clock_seconds = 0
    if clock and ":" in clock:
        try:
            parts = clock.split(":")
            clock_seconds = int(parts[0]) * 60 + int(parts[1])
        except (ValueError, IndexError):
            clock_seconds = 0

    period_lengths = {
        "nba": 720, "wnba": 600, "nhl": 1200, "nfl": 900,
        "ncaab": 1200, "ncaaf": 900,
        "mlb": 1,
    }

    is_soccer = sport in SOCCER_SPORTS

    if sport == "mlb":
        return min(max((period - 0.5) / total_periods, 0.0), 1.0)

    if is_soccer:
        # Soccer counts UP. Clock shows elapsed minutes.
        # Each half = 45 min = 2700 seconds. Total = 5400s.
        half_duration = 2700
        if period == 1:
            return min(clock_seconds / (half_duration * 2), 0.5)
        else:
            return min(0.5 + clock_seconds / (half_duration * 2), 1.0)

    # Count-down sports (NBA, NFL, NHL, NCAA)
    period_len = period_lengths.get(sport, 720)
    completed_time = (period - 1) * period_len
    elapsed_in_period = period_len - clock_seconds
    total_game_time = total_periods * period_len

    elapsed = completed_time + max(elapsed_in_period, 0)
    return min(max(elapsed / total_game_time, 0.0), 1.0)


def _evaluate_blowout(sport: str, lead: int, elapsed_pct: float) -> tuple[float, str]:
    """Check if game qualifies as a blowout. Returns (confidence, level)."""
    key = _get_threshold_key(sport)
    thresholds = WIN_THRESHOLDS.get(key, [])
    for min_lead, min_elapsed, conf, level in thresholds:
        if lead >= min_lead and elapsed_pct >= min_elapsed:
            return conf, level
    return 0.0, ""


async def _fetch_sport_games(session: aiohttp.ClientSession, sport: str) -> tuple[list[VerifiedGame], list[dict]]:
    """
    Fetch all live games for a single sport from ESPN.
    Returns (verified_blowouts, all_live_games_for_dashboard).
    """
    cfg = ESPN_SPORTS.get(sport)
    if not cfg:
        return [], []

    url = f"{ESPN_BASE}/{cfg['slug']}/{cfg['league']}/scoreboard"
    blowouts = []
    live_games = []

    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                logger.warning(f"ESPN {sport} returned status {resp.status}")
                return [], []
            data = await resp.json()
    except Exception as e:
        logger.error(f"ESPN fetch failed for {sport}: {e}")
        return [], []

    events = data.get("events", [])
    for event in events:
        try:
            competition = event["competitions"][0]
            status = competition.get("status", {})
            status_type = status.get("type", {})

            if status_type.get("state") != "in":
                continue

            competitors = competition.get("competitors", [])
            if len(competitors) < 2:
                continue

            home = away = None
            for comp in competitors:
                if comp.get("homeAway") == "home":
                    home = comp
                else:
                    away = comp

            if not home or not away:
                continue

            home_team = home.get("team", {}).get("displayName", "Unknown")
            away_team = away.get("team", {}).get("displayName", "Unknown")
            home_abbrev = home.get("team", {}).get("abbreviation", "???")
            away_abbrev = away.get("team", {}).get("abbreviation", "???")
            home_score = int(home.get("score", 0))
            away_score = int(away.get("score", 0))
            period = int(status.get("period", 1))
            clock = status.get("displayClock", "0:00")
            status_detail = status_type.get("detail", "")

            # Add to dashboard live games regardless of blowout status
            live_games.append({
                "espn_id": event.get("id", ""),
                "sport": sport,
                "home": f"{home_abbrev} {home_score}",
                "away": f"{away_abbrev} {away_score}",
                "detail": status_detail or f"P{period} {clock}",
            })

            lead = abs(home_score - away_score)
            if lead == 0:
                continue

            if home_score > away_score:
                leader, leader_abbrev = home_team, home_abbrev
                trailer, trailer_abbrev = away_team, away_abbrev
            else:
                leader, leader_abbrev = away_team, away_abbrev
                trailer, trailer_abbrev = home_team, home_abbrev

            elapsed_pct = _compute_elapsed_pct(sport, period, clock, status_detail)
            confidence, level = _evaluate_blowout(sport, lead, elapsed_pct)

            if confidence == 0.0:
                continue

            period_name = cfg.get("period_name", "P")
            score_line = (
                f"{leader_abbrev} {max(home_score, away_score)}-{min(home_score, away_score)} "
                f"{trailer_abbrev} | {period_name}{period} {clock} | {level} ({confidence:.1%})"
            )

            blowouts.append(VerifiedGame(
                espn_id=event.get("id", ""),
                sport=sport,
                home_team=home_team,
                away_team=away_team,
                home_abbrev=home_abbrev,
                away_abbrev=away_abbrev,
                home_score=home_score,
                away_score=away_score,
                leader=leader,
                leader_abbrev=leader_abbrev,
                trailer=trailer,
                trailer_abbrev=trailer_abbrev,
                lead=lead,
                period=period,
                clock=clock,
                elapsed_pct=elapsed_pct,
                confidence=confidence,
                level=level,
                score_line=score_line,
            ))

        except Exception as e:
            logger.error(f"Error parsing ESPN event in {sport}: {e}")
            continue

    return blowouts, live_games


async def fetch_verified_games() -> tuple[list[VerifiedGame], list[dict]]:
    """
    Fetch all live blowout games across all configured sports.
    Returns (blowouts sorted by confidence desc, all live games for dashboard).
    """
    async with aiohttp.ClientSession() as session:
        tasks = [_fetch_sport_games(session, sport) for sport in ESPN_SPORTS]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    all_blowouts = []
    all_live = []
    for result in results:
        if isinstance(result, Exception):
            logger.error(f"Sport fetch exception: {result}")
            continue
        blowouts, live = result
        all_blowouts.extend(blowouts)
        all_live.extend(live)

    all_blowouts.sort(key=lambda g: g.confidence, reverse=True)
    logger.info(f"ESPN scan: {len(all_blowouts)} blowouts, {len(all_live)} live games across {len(ESPN_SPORTS)} sports")
    return all_blowouts, all_live
