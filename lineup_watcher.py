"""
lineup_watcher.py — Pre-game lineup signal generator (v18.1 NEW)

Why this exists:
  Polymarket sports markets for J2, A-League, Championship, and other "sleeping lion"
  leagues often do NOT reprice when lineups are announced ~60 min before kickoff.
  Meanwhile Pinnacle and other sharp books DO reprice. That creates a 15-45 minute
  window where Polymarket is stale vs the true probability.

  This module fetches starting lineups for configured leagues ~60-90 min before
  kickoff, computes a lineup-impact score based on which high-minute players are
  in or out vs the expected lineup, and publishes a LineupSignal that the Edge
  engine consults when sizing bets.

Data source:
  api-football.com (v3). Free tier: 100 requests/day.
  Requires env var API_FOOTBALL_KEY.
  Flag-gated via LINEUP_WATCHER_ENABLED.

Endpoints used:
  GET /v3/fixtures?league={id}&season={year}&from={date}&to={date}
     → list of upcoming fixtures per league
  GET /v3/fixtures/lineups?fixture={id}
     → lineups (starting XI + subs) for a specific fixture
  GET /v3/injuries?league={id}&season={year}
     → optional, for pre-announcement injury awareness

Usage:
  from lineup_watcher import LineupWatcher
  lw = LineupWatcher()
  await lw.start(session)   # runs in background
  signal = lw.get_signal(home_team, away_team)  # returns dict or None

Signal shape:
  {
    "fixture_id": 1234,
    "kickoff_ts": 1713312000,
    "home": "Yokohama FC",
    "away": "Machida Zelvia",
    "home_impact": -0.08,   # negative = home weakened, bet AWAY
    "away_impact": +0.02,
    "net_home_edge_shift": -0.10,  # rough Δ probability: home_impact - away_impact
    "confidence": 0.7,      # how sure we are this is a real signal
    "detail": "Home missing top-scorer Tanaka (15 apps, 8 goals); away full strength",
    "computed_at": 1713311400,
  }
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
import aiohttp

from config import (
    API_FOOTBALL_KEY, API_FOOTBALL_BASE, LINEUP_WATCHER_ENABLED,
    LINEUP_WATCH_LEAGUES, LINEUP_CHECK_INTERVAL, LINEUP_PRE_GAME_WINDOW_MIN,
    LINEUP_FETCH_LEAD_MIN,
)
from teams import normalize as _nz

logger = logging.getLogger("lineup")


@dataclass
class PlayerImpact:
    player_id: int
    name: str
    position: str
    appearances: int = 0
    minutes: int = 0
    goals: int = 0
    assists: int = 0
    # Computed impact weight 0.0–1.0 (higher = more important)
    weight: float = 0.0


@dataclass
class FixtureWatch:
    fixture_id: int
    league_key: str            # our internal sport key (e.g. "j2")
    league_id: int             # api-football's league id
    season: int
    kickoff_ts: float          # unix timestamp
    home_team: str
    away_team: str
    home_team_id: int
    away_team_id: int
    home_expected: List[PlayerImpact] = field(default_factory=list)
    away_expected: List[PlayerImpact] = field(default_factory=list)
    lineup_fetched: bool = False
    signal: Optional[dict] = None


class LineupWatcher:
    """
    Background task that:
      1. Fetches upcoming fixtures for watched leagues every 4 hours
      2. Polls each fixture's lineup endpoint starting 75 min before kickoff
      3. When lineup is published, computes impact signal
      4. Makes signals accessible via get_signal(home, away)
    """

    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._fixtures: Dict[int, FixtureWatch] = {}           # fixture_id → watch
        self._signals_by_match: Dict[Tuple[str, str], dict] = {}  # (home_nz, away_nz) → signal
        self._api_calls_today: int = 0
        self._api_day_anchor: float = time.time()
        self._last_fixtures_refresh: float = 0
        self._player_cache: Dict[int, dict] = {}   # player_id → stats

    def is_enabled(self) -> bool:
        return bool(LINEUP_WATCHER_ENABLED and API_FOOTBALL_KEY)

    async def start(self, session: aiohttp.ClientSession):
        if self._task and not self._task.done():
            return
        if not self.is_enabled():
            logger.info("lineup_watcher: disabled (no API key or flag off)")
            return
        self._running = True
        self._task = asyncio.create_task(self._run(session))
        logger.info(f"lineup_watcher: started (watching {len(LINEUP_WATCH_LEAGUES)} leagues)")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def get_signal(self, home_team: str, away_team: str) -> Optional[dict]:
        """
        Look up a cached lineup signal for this matchup.
        Returns None if no signal or signal is stale (>3h old after kickoff).
        """
        key = (_nz(home_team), _nz(away_team))
        sig = self._signals_by_match.get(key)
        if not sig:
            return None
        # Expire 3h after kickoff
        if time.time() - sig.get("kickoff_ts", 0) > 3 * 3600:
            return None
        return sig

    def active_signals(self) -> List[dict]:
        """All current valid signals, for dashboard display."""
        now = time.time()
        return [
            s for s in self._signals_by_match.values()
            if now - s.get("kickoff_ts", 0) < 3 * 3600
        ]

    def api_budget(self) -> dict:
        """For dashboard — how many API calls used today, how many left."""
        self._maybe_reset_daily_counter()
        return {
            "used": self._api_calls_today,
            "limit": 100,
            "remaining": max(0, 100 - self._api_calls_today),
        }

    def _maybe_reset_daily_counter(self):
        if time.time() - self._api_day_anchor > 86400:
            self._api_calls_today = 0
            self._api_day_anchor = time.time()

    async def _get(self, session: aiohttp.ClientSession, path: str, params: dict) -> Optional[dict]:
        self._maybe_reset_daily_counter()
        if self._api_calls_today >= 95:
            logger.warning("lineup_watcher: approaching daily API limit, pausing")
            return None
        headers = {"x-apisports-key": API_FOOTBALL_KEY}
        url = f"{API_FOOTBALL_BASE}/{path.lstrip('/')}"
        try:
            async with session.get(url, headers=headers, params=params, timeout=15) as r:
                self._api_calls_today += 1
                if r.status == 429:
                    logger.warning("lineup_watcher: rate limited")
                    return None
                if r.status != 200:
                    logger.debug(f"api-football {path}: {r.status}")
                    return None
                data = await r.json()
                errors = data.get("errors")
                if errors and (isinstance(errors, dict) and errors) or (isinstance(errors, list) and errors):
                    logger.warning(f"api-football {path} errors: {errors}")
                return data
        except Exception as e:
            logger.debug(f"api-football {path}: {e}")
            return None

    async def _run(self, session: aiohttp.ClientSession):
        """Main loop: refresh fixtures periodically, poll lineups as kickoff approaches."""
        while self._running:
            try:
                now = time.time()

                # Refresh upcoming fixtures every 4 hours
                if now - self._last_fixtures_refresh > 4 * 3600:
                    await self._refresh_fixtures(session)
                    self._last_fixtures_refresh = now

                # For each watched fixture, check if we should fetch lineup
                due = [
                    f for f in self._fixtures.values()
                    if not f.lineup_fetched
                    and 0 < (f.kickoff_ts - now) <= LINEUP_FETCH_LEAD_MIN * 60
                ]
                # Sort by closest kickoff first
                due.sort(key=lambda f: f.kickoff_ts)

                for fx in due[:5]:  # cap concurrent fetches per cycle
                    await self._fetch_and_compute_lineup(session, fx)

                # Cleanup: drop fixtures more than 6h past kickoff
                stale_ids = [
                    fid for fid, f in self._fixtures.items()
                    if now - f.kickoff_ts > 6 * 3600
                ]
                for fid in stale_ids:
                    self._fixtures.pop(fid, None)

                await asyncio.sleep(LINEUP_CHECK_INTERVAL)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"lineup_watcher loop: {e}", exc_info=True)
                await asyncio.sleep(60)

    async def _refresh_fixtures(self, session: aiohttp.ClientSession):
        """Fetch upcoming 48h of fixtures for each watched league."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        in_2d = (datetime.now(timezone.utc).replace(hour=0) + timedelta(days=2)).strftime("%Y-%m-%d")
        added = 0
        for league_key, (league_id, season) in LINEUP_WATCH_LEAGUES.items():
            data = await self._get(session, "/fixtures", {
                "league": league_id,
                "season": season,
                "from": today,
                "to": in_2d,
            })
            if not data:
                continue
            for item in data.get("response", []):
                try:
                    fx_id = item["fixture"]["id"]
                    if fx_id in self._fixtures:
                        continue
                    ts = item["fixture"]["timestamp"]
                    if time.time() > ts:  # already kicked off
                        continue
                    home = item["teams"]["home"]["name"]
                    away = item["teams"]["away"]["name"]
                    self._fixtures[fx_id] = FixtureWatch(
                        fixture_id=fx_id,
                        league_key=league_key,
                        league_id=league_id,
                        season=season,
                        kickoff_ts=float(ts),
                        home_team=home,
                        away_team=away,
                        home_team_id=item["teams"]["home"]["id"],
                        away_team_id=item["teams"]["away"]["id"],
                    )
                    added += 1
                except (KeyError, TypeError) as e:
                    logger.debug(f"fixture parse: {e}")
        if added:
            logger.info(f"lineup_watcher: added {added} new fixtures ({len(self._fixtures)} watched)")

    async def _fetch_and_compute_lineup(self, session: aiohttp.ClientSession, fx: FixtureWatch):
        """Pull current lineup for this fixture. If available, compute impact."""
        data = await self._get(session, "/fixtures/lineups", {"fixture": fx.fixture_id})
        if not data:
            return
        response = data.get("response") or []
        if len(response) < 2:
            # Lineup not published yet; will retry next cycle
            return

        fx.lineup_fetched = True

        # Parse actual starting XI
        home_actual: List[dict] = []
        away_actual: List[dict] = []
        for team_lineup in response:
            try:
                tid = team_lineup["team"]["id"]
                starters = team_lineup.get("startXI", []) or []
                parsed = [
                    {"id": p["player"]["id"], "name": p["player"]["name"], "pos": p["player"].get("pos", "")}
                    for p in starters if p.get("player")
                ]
                if tid == fx.home_team_id:
                    home_actual = parsed
                elif tid == fx.away_team_id:
                    away_actual = parsed
            except (KeyError, TypeError):
                continue

        if not home_actual or not away_actual:
            return

        # We don't have "expected lineup" from api-football in a simple endpoint.
        # Proxy: compare to the team's most frequent starters from prior fixtures.
        # For now, use a simpler heuristic: count injuries/suspensions.
        home_impact = await self._compute_team_impact(session, fx.league_id, fx.season, fx.home_team_id, home_actual)
        away_impact = await self._compute_team_impact(session, fx.league_id, fx.season, fx.away_team_id, away_actual)

        net_home_shift = home_impact["impact"] - away_impact["impact"]

        # Confidence: higher when both sides were fully scored; lower when one side unknown
        confidence = min(home_impact["confidence"], away_impact["confidence"])

        signal = {
            "fixture_id": fx.fixture_id,
            "league": fx.league_key,
            "kickoff_ts": fx.kickoff_ts,
            "home": fx.home_team,
            "away": fx.away_team,
            "home_impact": round(home_impact["impact"], 4),
            "away_impact": round(away_impact["impact"], 4),
            "net_home_edge_shift": round(net_home_shift, 4),
            "confidence": round(confidence, 2),
            "detail": self._format_detail(fx, home_impact, away_impact),
            "computed_at": time.time(),
        }
        fx.signal = signal
        self._signals_by_match[(_nz(fx.home_team), _nz(fx.away_team))] = signal
        logger.info(
            f"lineup signal: {fx.home_team} vs {fx.away_team} | "
            f"net home shift {net_home_shift:+.3f} (conf {confidence:.2f})"
        )

    async def _compute_team_impact(
        self, session, league_id: int, season: int, team_id: int, actual_xi: List[dict]
    ) -> dict:
        """
        Heuristic impact calculation:
        - Pull team's injured list (1 API call per league per day, cached)
        - Starting XI that has N of the team's top-5 goal-scorers missing → negative impact
        - Confidence based on how much data we got
        """
        key = (league_id, season, team_id)
        if key not in self._player_cache:
            data = await self._get(session, "/players", {
                "league": league_id, "season": season, "team": team_id,
            })
            if data and data.get("response"):
                players = []
                for p in data["response"]:
                    try:
                        info = p["player"]
                        stats = p["statistics"][0] if p.get("statistics") else {}
                        apps = stats.get("games", {}).get("appearences") or 0
                        goals = stats.get("goals", {}).get("total") or 0
                        players.append({
                            "id": info["id"],
                            "name": info.get("name", ""),
                            "apps": apps,
                            "goals": goals,
                        })
                    except (KeyError, TypeError):
                        continue
                # Top contributors by a simple appearances + 3*goals score
                players.sort(key=lambda x: x["apps"] + 3 * x["goals"], reverse=True)
                self._player_cache[key] = {"top": players[:7]}
            else:
                self._player_cache[key] = {"top": []}

        top = self._player_cache[key].get("top", [])
        if not top:
            return {"impact": 0.0, "confidence": 0.0, "missing": [], "present": []}

        xi_ids = {p["id"] for p in actual_xi}
        missing = [p for p in top[:5] if p["id"] not in xi_ids]

        # Each missing top-5 player worth ~2% impact; top scorer worth up to ~4%
        impact = 0.0
        for p in missing:
            # Weight by rank: top (index 0) = 0.04, next = 0.03, ..., last = 0.02
            rank = top.index(p) if p in top else 4
            impact -= (0.04 - rank * 0.005)

        return {
            "impact": impact,
            "confidence": 0.8 if len(top) >= 5 else 0.5,
            "missing": [p["name"] for p in missing],
            "present": [p["name"] for p in top[:5] if p["id"] in xi_ids],
        }

    @staticmethod
    def _format_detail(fx, home_imp: dict, away_imp: dict) -> str:
        parts = []
        if home_imp["missing"]:
            parts.append(f"{fx.home_team} missing {', '.join(home_imp['missing'][:3])}")
        if away_imp["missing"]:
            parts.append(f"{fx.away_team} missing {', '.join(away_imp['missing'][:3])}")
        if not parts:
            return f"{fx.home_team} vs {fx.away_team}: no impact players missing"
        return "; ".join(parts)
