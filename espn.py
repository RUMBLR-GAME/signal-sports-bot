"""
espn.py — ESPN Live Score Verification Engine

Free API. No key needed. Real-time scores across 10 sports.
Calculates verified win probability from lead + game progress.
"""

import re
import requests
from dataclasses import dataclass
from typing import Optional
import config


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
    leader_team: str
    trailer_team: str
    lead: int
    period: str
    clock: str
    elapsed_pct: float
    detail: str
    confidence: float
    level: str
    score_line: str


def fetch_verified_games() -> list[VerifiedGame]:
    verified = []
    for sport_key, sport_cfg in config.ESPN_SPORTS.items():
        try:
            r = requests.get(sport_cfg["url"], timeout=12)
            if not r.ok:
                continue
            for event in r.json().get("events", []):
                game = _parse_game(event, sport_key)
                if game and game.confidence >= 0.975:
                    verified.append(game)
        except Exception:
            continue
    verified.sort(key=lambda g: g.confidence, reverse=True)
    return verified


def _parse_game(event: dict, sport: str) -> Optional[VerifiedGame]:
    status = event.get("status", {}).get("type", {})
    state = status.get("state", "")
    if state not in ("in", "post"):
        return None

    detail = status.get("detail", "")
    clock = status.get("displayClock", "")
    comp = (event.get("competitions") or [{}])[0]
    competitors = comp.get("competitors", [])
    if len(competitors) < 2:
        return None

    home = away = None
    for c in competitors:
        if c.get("homeAway") == "home":
            home = c
        else:
            away = c
    if not home or not away:
        return None

    home_team = home.get("team", {}).get("displayName", "Unknown")
    away_team = away.get("team", {}).get("displayName", "Unknown")
    home_abbrev = home.get("team", {}).get("abbreviation", "???")
    away_abbrev = away.get("team", {}).get("abbreviation", "???")

    try:
        home_score = int(home.get("score", "0"))
        away_score = int(away.get("score", "0"))
    except (ValueError, TypeError):
        return None

    lead = abs(home_score - away_score)
    if lead == 0:
        return None

    if home_score > away_score:
        leader, leader_team, trailer_team = "home", home_team, away_team
    else:
        leader, leader_team, trailer_team = "away", away_team, home_team

    elapsed_pct = _calc_elapsed(sport, detail, state)
    confidence, level = _check_thresholds(sport, lead, elapsed_pct, state)

    if confidence < 0.975:
        return None

    period = _extract_period(detail, sport)
    score_line = f"{home_abbrev} {home_score} - {away_abbrev} {away_score} {period} {clock}".strip()

    return VerifiedGame(
        espn_id=str(event.get("id", "")), sport=sport,
        home_team=home_team, away_team=away_team,
        home_abbrev=home_abbrev, away_abbrev=away_abbrev,
        home_score=home_score, away_score=away_score,
        leader=leader, leader_team=leader_team, trailer_team=trailer_team,
        lead=lead, period=period, clock=clock,
        elapsed_pct=elapsed_pct, detail=detail,
        confidence=confidence, level=level, score_line=score_line,
    )


def _calc_elapsed(sport: str, detail: str, state: str) -> float:
    if state == "post":
        return 1.0
    d = detail.lower()
    clock_mins = _parse_clock(detail)

    if sport in ("nba", "nfl", "ncaaf"):
        qtr_len = 12.0 if sport == "nba" else 15.0
        for qi, label in enumerate(["1st", "2nd", "3rd", "4th"]):
            if label in d:
                base = qi / 4.0
                if clock_mins is not None:
                    return base + ((qtr_len - clock_mins) / qtr_len) / 4.0
                return base + 0.125
        if "ot" in d or "overtime" in d:
            return 0.96
        if "half" in d:
            return 0.50

    elif sport in ("ncaab", "wnba"):
        for hi, label in enumerate(["1st", "2nd"]):
            if label in d:
                base = hi * 0.5
                if clock_mins is not None:
                    return base + ((20.0 - clock_mins) / 20.0) * 0.5
                return base + 0.25
        if "ot" in d:
            return 0.96

    elif sport == "nhl":
        for pi, label in enumerate(["1st", "2nd", "3rd"]):
            if label in d:
                base = pi / 3.0
                if clock_mins is not None:
                    return base + ((20.0 - clock_mins) / 20.0) / 3.0
                return base + 0.167
        if "ot" in d:
            return 0.96

    elif sport == "mlb":
        m = re.search(r'(\w+)\s+(\d+)(st|nd|rd|th)', d)
        if m:
            half = 0 if m.group(1) in ("top", "mid") else 1
            inning = int(m.group(2))
            return min(((inning - 1) * 2 + half) / 18.0, 1.0)
        m2 = re.search(r'(\d+)(st|nd|rd|th)', d)
        if m2:
            return min((int(m2.group(1)) - 1) / 9.0, 1.0)

    elif sport in ("epl", "mls", "liga"):
        m = re.search(r"(\d+)'", detail)
        if m:
            return min(int(m.group(1)) / 90.0, 1.0)
        if "half" in d:
            return 0.50

    return 0.5


def _check_thresholds(sport: str, lead: int, elapsed: float, state: str) -> tuple[float, str]:
    if state == "post" and lead > 0:
        return 1.0, "final"
    lookup = "soccer" if sport in ("epl", "mls", "liga") else sport
    for min_lead, min_elapsed, conf, level in config.WIN_THRESHOLDS.get(lookup, []):
        if lead >= min_lead and elapsed >= min_elapsed:
            return conf, level
    return 0.50, "none"


def _extract_period(detail: str, sport: str) -> str:
    d = detail.lower()
    if sport in ("nba", "nfl", "ncaaf"):
        for q in ["4th", "3rd", "2nd", "1st"]:
            if q in d:
                return q
    elif sport in ("ncaab", "wnba"):
        if "2nd" in d: return "2H"
        if "1st" in d: return "1H"
    elif sport == "nhl":
        for p in ["3rd", "2nd", "1st"]:
            if p in d: return p
    elif sport == "mlb":
        m = re.search(r'(\w+)\s+(\d+)(st|nd|rd|th)', d)
        if m: return f"{m.group(1).capitalize()} {m.group(2)}{m.group(3)}"
    elif sport in ("epl", "mls", "liga"):
        m = re.search(r"(\d+)'", detail)
        if m: return f"{m.group(1)}'"
    if "ot" in d: return "OT"
    if "final" in d: return "Final"
    if "half" in d: return "Half"
    return detail[:20]


def _parse_clock(detail: str) -> Optional[float]:
    m = re.search(r'(\d{1,2}):(\d{2})', detail)
    return int(m.group(1)) + int(m.group(2)) / 60.0 if m else None


def team_search_terms(full_name: str, abbrev: str) -> list[str]:
    terms = set()
    fl = full_name.lower().strip()
    terms.add(fl)
    parts = fl.split()
    if parts:
        terms.add(parts[-1])
    if abbrev:
        terms.add(abbrev.lower())
    return [t for t in terms if len(t) > 2]
