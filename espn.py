"""
espn.py — ESPN Scoreboard Fetcher (v18)

Role in v18: FALLBACK / cross-check for Polymarket Sports WebSocket.
Also still the primary source of pre-game moneyline odds for US sports
(ESPN embeds FanDuel/ESPN BET odds in scoreboard payloads).

v17 issues fixed:
  • Created+destroyed aiohttp session per call — now uses caller's session
  • Fired 41 parallel requests twice per cycle — now single fetch_all()
    returns (blowouts, live, pregame_odds) from one pass
  • No bounded timeouts at the gather() level — now has a hard ceiling
"""
import asyncio
import logging
from dataclasses import dataclass
from typing import Optional, List, Tuple
import aiohttp

from config import ESPN_SPORTS, WIN_THRESHOLDS, SOCCER_SPORTS, NBA_MIN_LEAD_Q4, NBA_MAX_CLOCK_Q4_SEC

logger = logging.getLogger("espn")
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"
PER_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=8)
GATHER_TIMEOUT = 20  # total for parallel scan


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
    clock_sec: int
    elapsed_pct: float
    confidence: float
    level: str
    score_line: str


@dataclass
class GameOdds:
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
    status: str
    commence_time: str


def _threshold_key(sport: str) -> str:
    return "soccer" if sport in SOCCER_SPORTS else sport


def _american_to_decimal(ml: int) -> float:
    if ml > 0:
        return 1.0 + ml / 100.0
    if ml < 0:
        return 1.0 + 100.0 / abs(ml)
    return 0.0


def _devig(home_ml: int, away_ml: int) -> Tuple[float, float]:
    hd, ad = _american_to_decimal(home_ml), _american_to_decimal(away_ml)
    if hd <= 1.0 or ad <= 1.0:
        return 0.0, 0.0
    hi, ai = 1.0 / hd, 1.0 / ad
    t = hi + ai
    return (round(hi / t, 4), round(ai / t, 4)) if t > 0 else (0.0, 0.0)


def _clock_to_seconds(clock: str) -> int:
    if not clock or ":" not in clock:
        return 0
    try:
        m, s = clock.split(":")
        return int(m) * 60 + int(s)
    except (ValueError, IndexError):
        return 0


def _elapsed_pct(sport: str, period: int, clock: str, detail: str) -> float:
    cfg = ESPN_SPORTS.get(sport, {})
    total_p = cfg.get("periods", 4)
    dl = (detail or "").lower()
    if "final" in dl or "end" in dl:
        return 1.0
    if "halftime" in dl:
        return 0.5
    cs = _clock_to_seconds(clock)
    if sport == "mlb":
        return min(max((period - 0.5) / total_p, 0.0), 1.0)
    if sport in SOCCER_SPORTS:
        # ESPN soccer clock counts UP from 0, period 1=first half, 2=second
        # Use total minutes = 90 (ignore stoppage time for conservative estimate)
        if period == 1:
            return min(cs / 5400, 0.5)
        return min(0.5 + cs / 5400, 1.0)
    pl = {"nba": 720, "wnba": 600, "nhl": 1200, "nfl": 900, "ncaab": 1200, "ncaaf": 900}
    plen = pl.get(sport, 720)
    elapsed = (period - 1) * plen + max(plen - cs, 0)
    return min(max(elapsed / (total_p * plen), 0.0), 1.0)


def _evaluate_blowout(sport: str, lead: int, elapsed_pct: float) -> Tuple[float, str]:
    for ml, me, conf, level in WIN_THRESHOLDS.get(_threshold_key(sport), []):
        if lead >= ml and elapsed_pct >= me:
            return conf, level
    return 0.0, ""


def _nba_safety_check(sport: str, period: int, clock_sec: int, lead: int) -> bool:
    """
    NBA only: extra filter for "20 is the new 12" era.
    Returns True if the blowout passes the tighter rules.
    Applied on top of the standard threshold.
    """
    if sport != "nba":
        return True
    if period < 4:
        return False
    if lead < NBA_MIN_LEAD_Q4:
        return False
    if clock_sec > NBA_MAX_CLOCK_Q4_SEC:
        return False
    return True


async def _fetch_sport(
    session: aiohttp.ClientSession, sport: str
) -> Tuple[List[VerifiedGame], List[dict], List[GameOdds]]:
    """Single fetch per sport returning (blowouts, live_games, pregame_odds)."""
    cfg = ESPN_SPORTS.get(sport)
    if not cfg:
        return [], [], []
    url = f"{ESPN_BASE}/{cfg['slug']}/{cfg['league']}/scoreboard"
    try:
        async with session.get(url, timeout=PER_REQUEST_TIMEOUT) as resp:
            if resp.status != 200:
                return [], [], []
            data = await resp.json()
    except Exception as e:
        logger.debug(f"ESPN {sport}: {e}")
        return [], [], []

    blowouts: List[VerifiedGame] = []
    live: List[dict] = []
    odds: List[GameOdds] = []

    for event in data.get("events", []):
        try:
            eid = event.get("id", "")
            commence = event.get("date", "")
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
            hs = int(home.get("score", 0) or 0)
            aws = int(away.get("score", 0) or 0)

            # Moneyline odds (US sports: NBA/WNBA/NFL/MLB/NHL/MLS/NCAA)
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

            # Live state
            if state == "in":
                period = int(st.get("period", 1) or 1)
                clock = st.get("displayClock", "0:00")
                clock_sec = _clock_to_seconds(clock)
                detail = stype.get("detail", "")
                live.append({
                    "espn_id": eid, "sport": sport,
                    "home_team": ht, "away_team": at,
                    "home_abbrev": ha, "away_abbrev": aa,
                    "home_score": hs, "away_score": aws,
                    "detail": detail or f"P{period} {clock}",
                    "period": period, "clock": clock, "clock_sec": clock_sec,
                    "state": state,
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
                    if conf > 0 and _nba_safety_check(sport, period, clock_sec, lead):
                        pn = cfg.get("period_name", "P")
                        sl = f"{la} {max(hs,aws)}-{min(hs,aws)} {ta} | {pn}{period} {clock} | {lvl} ({conf:.1%})"
                        blowouts.append(VerifiedGame(
                            espn_id=eid, sport=sport, home_team=ht, away_team=at,
                            home_abbrev=ha, away_abbrev=aa, home_score=hs, away_score=aws,
                            leader=ldr, leader_abbrev=la, trailer=tr, trailer_abbrev=ta,
                            lead=lead, period=period, clock=clock, clock_sec=clock_sec,
                            elapsed_pct=ep, confidence=conf, level=lvl, score_line=sl,
                        ))
        except Exception as e:
            logger.debug(f"ESPN {sport} parse: {e}")
    return blowouts, live, odds


async def fetch_all(session: aiohttp.ClientSession) -> Tuple[List[VerifiedGame], List[dict], List[GameOdds]]:
    """
    Single call fetches every league in parallel and returns
    (blowouts, live_games, pregame_odds). Shared session = connection reuse.
    """
    tasks = [_fetch_sport(session, sp) for sp in ESPN_SPORTS]
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=GATHER_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning("ESPN gather timeout")
        return [], [], []

    bl: List[VerifiedGame] = []
    lv: List[dict] = []
    od: List[GameOdds] = []
    for r in results:
        if isinstance(r, Exception):
            continue
        b, l, o = r
        bl.extend(b)
        lv.extend(l)
        od.extend(o)
    bl.sort(key=lambda g: g.confidence, reverse=True)
    return bl, lv, od
