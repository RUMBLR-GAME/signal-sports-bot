"""
espn.py — ESPN Live Scores + Embedded Odds + Game Start Times
Fetches scores, sportsbook odds, and game start times from ESPN's free API.

Key data for each engine:
  Harvest: live scores, blowout detection
  Edge Finder: sportsbook odds (FanDuel/ESPN BET), game start times
  Dashboard: live game ticker
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


@dataclass
class GameOdds:
    """Sportsbook odds + game start time for Edge Finder convergence trades."""
    espn_id: str
    sport: str
    home_team: str
    away_team: str
    home_abbrev: str
    away_abbrev: str
    provider: str
    home_ml: int
    away_ml: int
    home_prob: float
    away_prob: float
    spread: float
    status: str               # "pre" or "in"
    commence_time: str         # ISO 8601 game start time (critical for Edge Finder)


from teams import generate_search_terms as team_search_terms


def _threshold_key(sport: str) -> str:
    return "soccer" if sport in SOCCER_SPORTS else sport


def _american_to_decimal(ml: int) -> float:
    if ml > 0:
        return 1.0 + ml / 100.0
    elif ml < 0:
        return 1.0 + 100.0 / abs(ml)
    return 0.0


def _devig(home_ml: int, away_ml: int) -> tuple[float, float]:
    hd, ad = _american_to_decimal(home_ml), _american_to_decimal(away_ml)
    if hd <= 1.0 or ad <= 1.0:
        return 0.0, 0.0
    hi, ai = 1.0 / hd, 1.0 / ad
    t = hi + ai
    return (round(hi / t, 4), round(ai / t, 4)) if t > 0 else (0.0, 0.0)


def _elapsed_pct(sport, period, clock, detail):
    cfg = ESPN_SPORTS.get(sport, {})
    total_p = cfg.get("periods", 4)
    dl = (detail or "").lower()
    if "final" in dl or "end" in dl:
        return 1.0
    if "halftime" in dl:
        return 0.5
    cs = 0
    if clock and ":" in clock:
        try:
            p = clock.split(":")
            cs = int(p[0]) * 60 + int(p[1])
        except (ValueError, IndexError):
            cs = 0
    pl = {"nba":720,"wnba":600,"nhl":1200,"nfl":900,"ncaab":1200,"ncaaf":900}
    if sport == "mlb":
        return min(max((period - 0.5) / total_p, 0.0), 1.0)
    if sport in SOCCER_SPORTS:
        return min(cs / 5400, 0.5) if period == 1 else min(0.5 + cs / 5400, 1.0)
    plen = pl.get(sport, 720)
    elapsed = (period - 1) * plen + max(plen - cs, 0)
    return min(max(elapsed / (total_p * plen), 0.0), 1.0)


def _evaluate_blowout(sport, lead, elapsed_pct):
    for ml, me, conf, level in WIN_THRESHOLDS.get(_threshold_key(sport), []):
        if lead >= ml and elapsed_pct >= me:
            return conf, level
    return 0.0, ""


async def _fetch_sport(session, sport):
    cfg = ESPN_SPORTS.get(sport)
    if not cfg:
        return [], [], []
    url = f"{ESPN_BASE}/{cfg['slug']}/{cfg['league']}/scoreboard"
    blowouts, live, odds = [], [], []
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return [], [], []
            data = await resp.json()
    except Exception as e:
        logger.error(f"ESPN {sport}: {e}")
        return [], [], []

    for event in data.get("events", []):
        try:
            eid = event.get("id", "")
            commence = event.get("date", "")  # ISO 8601 game start time
            comp = event["competitions"][0]
            st = comp.get("status", {})
            stype = st.get("type", {})
            state = stype.get("state", "")

            comps = comp.get("competitors", [])
            if len(comps) < 2:
                continue
            home = away = None
            for c in comps:
                if c.get("homeAway") == "home":
                    home = c
                else:
                    away = c
            if not home or not away:
                continue

            ht = home.get("team", {}).get("displayName", "")
            at = away.get("team", {}).get("displayName", "")
            ha = home.get("team", {}).get("abbreviation", "")
            aa = away.get("team", {}).get("abbreviation", "")
            hs = int(home.get("score", 0))
            aws = int(away.get("score", 0))

            # Parse odds for Edge Finder (pre-game AND early in-game)
            if state in ("pre", "in"):
                for o in comp.get("odds", []):
                    try:
                        prov = o.get("provider", {}).get("name", "")
                        hml = o.get("homeTeamOdds", {}).get("moneyLine")
                        aml = o.get("awayTeamOdds", {}).get("moneyLine")
                        if not prov or hml is None or aml is None:
                            continue
                        hml, aml = int(hml), int(aml)
                        hp, ap = _devig(hml, aml)
                        if hp == 0:
                            continue
                        odds.append(GameOdds(
                            espn_id=eid, sport=sport,
                            home_team=ht, away_team=at,
                            home_abbrev=ha, away_abbrev=aa,
                            provider=prov, home_ml=hml, away_ml=aml,
                            home_prob=hp, away_prob=ap,
                            spread=float(o.get("spread", 0) or 0),
                            status=state, commence_time=commence,
                        ))
                    except Exception:
                        continue

            # Live games for dashboard
            if state == "in":
                period = int(st.get("period", 1))
                clock = st.get("displayClock", "0:00")
                detail = stype.get("detail", "")
                live.append({
                    "espn_id": eid, "sport": sport,
                    "home_team": ht, "away_team": at,
                    "home_abbrev": ha, "away_abbrev": aa,
                    "home_score": hs, "away_score": aws,
                    "detail": detail or f"P{period} {clock}",
                    "period": period, "clock": clock,
                    # Legacy fields for backwards compat
                    "home": f"{ha} {hs}", "away": f"{aa} {aws}",
                })

                # Blowout detection
                lead = abs(hs - aws)
                if lead > 0:
                    if hs > aws:
                        ldr, la, tr, ta = ht, ha, at, aa
                    else:
                        ldr, la, tr, ta = at, aa, ht, ha
                    ep = _elapsed_pct(sport, period, clock, detail)
                    conf, lvl = _evaluate_blowout(sport, lead, ep)
                    if conf > 0:
                        pn = cfg.get("period_name", "P")
                        sl = f"{la} {max(hs,aws)}-{min(hs,aws)} {ta} | {pn}{period} {clock} | {lvl} ({conf:.1%})"
                        blowouts.append(VerifiedGame(
                            espn_id=eid, sport=sport, home_team=ht, away_team=at,
                            home_abbrev=ha, away_abbrev=aa, home_score=hs, away_score=aws,
                            leader=ldr, leader_abbrev=la, trailer=tr, trailer_abbrev=ta,
                            lead=lead, period=period, clock=clock, elapsed_pct=ep,
                            confidence=conf, level=lvl, score_line=sl,
                        ))
        except Exception as e:
            logger.error(f"ESPN {sport} parse: {e}")
    return blowouts, live, odds


async def fetch_verified_games():
    async with aiohttp.ClientSession() as s:
        results = await asyncio.gather(*[_fetch_sport(s, sp) for sp in ESPN_SPORTS], return_exceptions=True)
    bl, lv = [], []
    for r in results:
        if isinstance(r, Exception):
            continue
        b, l, _ = r
        bl.extend(b)
        lv.extend(l)
    bl.sort(key=lambda g: g.confidence, reverse=True)
    return bl, lv


async def fetch_pregame_odds():
    async with aiohttp.ClientSession() as s:
        results = await asyncio.gather(*[_fetch_sport(s, sp) for sp in ESPN_SPORTS], return_exceptions=True)
    all_odds = []
    for r in results:
        if isinstance(r, Exception):
            continue
        _, _, odds = r
        all_odds.extend(odds)
    logger.info(f"ESPN odds: {len(all_odds)} game-provider combos")
    return all_odds
