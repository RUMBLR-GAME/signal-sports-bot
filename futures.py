"""
futures.py — Polymarket futures / outright markets scanner (v22 stub)

STATUS: Infrastructure scaffolded but signal generation DISABLED until we have
a confirmed source of sharp bookmaker outright odds. This module compiles,
integrates with the bot loop, and returns empty signal lists — so enabling the
scanner just needs a truth source plugin.

Why stub vs full implementation: Without sharp bookmaker outright odds, there's
no trustworthy "true probability" to compare Polymarket prices against. Using
Polymarket's own prices as truth is circular. Using Pinnacle game-day odds for
a year-long futures market is nonsensical. We'd rather return no signals than
fake ones.

What this module DOES do when enabled:
  - Discovers Polymarket futures via Gamma API /events with championship-like tags
  - Filters by liquidity, time-to-settlement, market count
  - Maintains a cache of discovered futures events
  - Exposes diagnostics to /api/state for dashboard visibility
  - Has exit logic hooks matching edge.py pattern

What it does NOT do yet (pending truth source):
  - Calculate edge (needs sharp book outright odds)
  - Generate trade signals
  - Auto-convergence exits (no reference price)

When a truth source is wired in, replace `_get_futures_truth_prob()` with the
real implementation and signal generation will activate immediately.
"""
import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, List, Tuple

import aiohttp

from config import (
    FUTURES_ENABLED, FUTURES_SCAN_INTERVAL, FUTURES_MIN_EDGE,
    FUTURES_MAX_POSITION_PCT, FUTURES_MAX_TOTAL_PCT, FUTURES_MIN_LIQUIDITY,
    FUTURES_EXIT_DAYS_BEFORE, FUTURES_CONVERGENCE_PCT, FUTURES_MAX_HOLD_DAYS,
    GAMMA_API, EDGE_FEE_AWARE,
)
from clob import ClobInterface
from positions import PositionManager

logger = logging.getLogger("futures")


@dataclass
class FuturesSignal:
    """Signal emitted by futures scanner for execute_signal()."""
    engine: str                  # always "futures"
    sport: str                   # e.g., "soccer", "nfl"
    espn_id: str                 # empty for futures (no espn mapping)
    condition_id: str
    market_question: str
    team: str                    # specific team/entity
    bet_outcome: str             # e.g., "Yes" or "Team X"
    outcome_idx: int
    token_id: str
    clob_price: float
    true_prob: float
    edge: float
    provider: str
    moneyline: int
    bet_size: float
    confidence: float
    commence_time: str           # event endDate (resolution target)
    sizing_reason: str
    score_line: str = ""         # unused for futures


# ─── Futures market keywords ─────────────────────────────────────────────────
# Polymarket events whose titles contain these keywords are likely futures.
# Distinct from game-day markets (which are filtered OUT by FUTURES_BLOCK).
FUTURES_KEYWORDS = {
    "champion", "winner", "mvp", "cup winner", "series winner",
    "trophy", "finals winner", "title winner",
}


def _is_futures_event(event: dict) -> bool:
    """Check if a Gamma event is a futures/outright market."""
    title = (event.get("title") or "").lower()
    slug = (event.get("slug") or "").lower()
    combined = title + " " + slug
    return any(kw in combined for kw in FUTURES_KEYWORDS)


def _days_until(iso_date: str) -> Optional[float]:
    """Return days between now and an ISO date string. None if unparseable."""
    if not iso_date:
        return None
    try:
        end = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return (end - now).total_seconds() / 86400.0
    except (ValueError, TypeError):
        return None


async def _get_futures_truth_prob(sport: str, team: str, market_question: str) -> Optional[float]:
    """STUB: Returns None until a real truth source is wired up.

    When wiring a truth source, return the sharp-bookmaker implied probability
    for this team to win this event. Use de-vigged odds from Pinnacle/Bet365
    outright markets.

    Candidates for truth source:
      1. odds-api.io outrights (pending confirmation this is supported)
      2. the-odds-api.com (separate subscription, $10-30/mo starter)
      3. Betfair Exchange (cleanest truth source, harder API auth)
      4. Pinnacle direct (hardest to access)

    Do NOT fall through to "Polymarket midpoint" — self-referential, no edge.
    """
    return None


async def discover_futures_events(
    session: aiohttp.ClientSession, max_age_sec: float = 1800
) -> List[dict]:
    """Fetch active Polymarket events and filter to futures-like ones.

    Results cached in-module for max_age_sec to avoid spamming Gamma.
    Sorted by volume descending (liquidity proxy).
    """
    now = time.time()
    if _discovery_cache["ts"] and (now - _discovery_cache["ts"]) < max_age_sec:
        return _discovery_cache["events"]

    url = f"{GAMMA_API}/events?active=true&closed=false&limit=100&order=volume&ascending=false"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                logger.warning(f"Gamma /events returned {resp.status}")
                return _discovery_cache["events"]  # stale is better than empty
            data = await resp.json()
    except Exception as e:
        logger.warning(f"Gamma /events fetch error: {e}")
        return _discovery_cache["events"]

    futures = []
    for ev in data:
        if not _is_futures_event(ev):
            continue
        end_date = ev.get("endDate") or ev.get("end_date") or ""
        days = _days_until(end_date)
        if days is None or days < FUTURES_EXIT_DAYS_BEFORE or days > 365:
            continue
        volume = 0.0
        try:
            volume = float(ev.get("volume", 0) or 0)
        except (TypeError, ValueError):
            volume = 0.0
        if volume < 50000:  # skip illiquid
            continue
        n_markets = len(ev.get("markets") or [])
        if n_markets < 5:  # skip binary "will X happen" futures — usually thin
            continue
        futures.append({
            "title": ev.get("title", ""),
            "slug": ev.get("slug", ""),
            "end_date": end_date,
            "days_until": days,
            "volume": volume,
            "n_markets": n_markets,
            "markets": ev.get("markets", []),
        })

    _discovery_cache["ts"] = now
    _discovery_cache["events"] = futures
    logger.info(f"Futures discovery: {len(futures)} events pass filters")
    return futures


# Module-level cache (not per-instance — this module is a singleton)
_discovery_cache: dict = {"ts": 0.0, "events": []}


async def scan_futures(
    clob: ClobInterface,
    positions: PositionManager,
    session: aiohttp.ClientSession,
) -> Tuple[List[FuturesSignal], dict]:
    """Main futures scan entrypoint. Returns (signals, diag).

    Currently returns empty signals because _get_futures_truth_prob is a stub.
    All other logic is live and will activate the moment truth source is real.
    """
    diag = {
        "enabled": FUTURES_ENABLED,
        "truth_source": "STUB (no outright odds wired)",
        "events_scanned": 0,
        "markets_scanned": 0,
        "no_truth": 0,
        "skip_low_edge": 0,
        "skip_low_liquidity": 0,
        "signals": 0,
    }
    if not FUTURES_ENABLED:
        return [], diag

    events = await discover_futures_events(session)
    diag["events_scanned"] = len(events)
    signals: List[FuturesSignal] = []

    for event in events:
        # Total cap check — don't scan if we're already at max futures exposure
        current_futures_exposure = _deployed_in_futures(positions)
        if current_futures_exposure >= positions.equity * FUTURES_MAX_TOTAL_PCT:
            diag["cap_reached"] = True
            break

        for market in event.get("markets", []):
            diag["markets_scanned"] += 1

            # Resolution risk check applies here too
            q_lower = (market.get("question") or "").lower()
            from config import RESOLUTION_RISK_KEYWORDS, RESOLUTION_RISK_FILTER_ENABLED
            if RESOLUTION_RISK_FILTER_ENABLED and any(kw in q_lower for kw in RESOLUTION_RISK_KEYWORDS):
                continue

            # Fetch current Polymarket price
            token_ids = market.get("clobTokenIds") or []
            if isinstance(token_ids, str):
                try:
                    import json as _json
                    token_ids = _json.loads(token_ids)
                except Exception:
                    continue
            if not token_ids:
                continue
            yes_tid = token_ids[0] if len(token_ids) > 0 else None
            if not yes_tid:
                continue

            try:
                poly_price = await clob.get_price(yes_tid, "BUY")
            except Exception:
                continue
            if poly_price is None or poly_price <= 0 or poly_price >= 1:
                continue

            # Ask for truth probability (stub returns None, so we always bail here)
            team = market.get("groupItemTitle") or market.get("title") or ""
            truth = await _get_futures_truth_prob(
                sport=_infer_sport(event.get("title", "")),
                team=team,
                market_question=market.get("question", ""),
            )
            if truth is None:
                diag["no_truth"] += 1
                continue

            raw_edge = truth - poly_price
            # Apply fee-awareness when computing net edge (same logic as edge.py)
            if EDGE_FEE_AWARE:
                from edge import net_edge_after_costs
                net_edge = net_edge_after_costs(raw_edge, poly_price)
            else:
                net_edge = raw_edge

            if net_edge < FUTURES_MIN_EDGE:
                diag["skip_low_edge"] += 1
                continue

            # Liquidity check
            depth = await clob.depth_at_price(yes_tid, poly_price, "BUY")
            if depth < FUTURES_MIN_LIQUIDITY:
                diag["skip_low_liquidity"] += 1
                continue

            # Sizing — simple for now, will refine when live
            size = min(
                positions.equity * FUTURES_MAX_POSITION_PCT,
                positions.equity * FUTURES_MAX_TOTAL_PCT - current_futures_exposure,
            )
            if size < 5:
                continue

            signals.append(FuturesSignal(
                engine="futures",
                sport=_infer_sport(event.get("title", "")),
                espn_id="",
                condition_id=market.get("conditionId", ""),
                market_question=market.get("question", ""),
                team=team,
                bet_outcome="Yes",
                outcome_idx=0,
                token_id=yes_tid,
                clob_price=poly_price,
                true_prob=truth,
                edge=net_edge,
                provider="futures_stub",
                moneyline=0,
                bet_size=round(size, 2),
                confidence=truth,
                commence_time=event.get("end_date", ""),
                sizing_reason=f"futures {net_edge*100:.1f}% edge",
            ))
            diag["signals"] += 1

    return signals, diag


def _deployed_in_futures(positions: PositionManager) -> float:
    """Sum $ currently in futures positions."""
    return sum(p.cost for p in positions.get_open_positions() if p.engine == "futures")


def _infer_sport(event_title: str) -> str:
    """Best-effort sport inference from event title."""
    t = (event_title or "").lower()
    if "world cup" in t or "premier league" in t or "champions league" in t or "euro" in t:
        return "soccer"
    if "super bowl" in t or "nfl" in t:
        return "nfl"
    if "nba" in t or "lakers" in t:
        return "nba"
    if "world series" in t or "mlb" in t:
        return "mlb"
    if "stanley cup" in t or "nhl" in t:
        return "nhl"
    return "other"


async def check_futures_exits(
    clob: ClobInterface, positions: PositionManager,
) -> int:
    """Exit-time policies for futures positions.
       Returns count of positions exited.
       Exit conditions (priority order):
         1. Days until resolution < FUTURES_EXIT_DAYS_BEFORE
         2. Price reached convergence threshold (FUTURES_CONVERGENCE_PCT of max edge)
         3. Max hold days exceeded
         4. Stop loss triggered
    """
    if not FUTURES_ENABLED:
        return 0

    exits = 0
    for p in list(positions.get_open_positions()):
        if p.engine != "futures":
            continue

        # 1. Time-to-resolution exit
        days = _days_until(p.game_start_time)
        if days is not None and days < FUTURES_EXIT_DAYS_BEFORE:
            # Get current mark
            try:
                mark = await clob.get_price(p.token_id, "SELL")
            except Exception:
                mark = p.current_price or p.entry_price
            if mark is None:
                mark = p.current_price or p.entry_price
            await positions.force_close(p.id, mark, f"resolution_approaches ({days:.1f}d)")
            exits += 1
            continue

        # 3. Max hold exceeded
        held_days = (time.time() - (p.filled_at or p.opened_at)) / 86400.0
        if held_days > FUTURES_MAX_HOLD_DAYS:
            try:
                mark = await clob.get_price(p.token_id, "SELL") or p.entry_price
            except Exception:
                mark = p.entry_price
            await positions.force_close(p.id, mark, f"max_hold ({held_days:.0f}d)")
            exits += 1
            continue

        # 2. Convergence exit — captured enough of theoretical max profit
        entry = p.fill_price or p.entry_price
        if entry > 0:
            try:
                mark = await clob.get_price(p.token_id, "SELL")
            except Exception:
                mark = None
            if mark is not None and mark > entry:
                captured_pct = (mark - entry) / max(1.0 - entry, 0.01)
                if captured_pct >= FUTURES_CONVERGENCE_PCT:
                    await positions.force_close(
                        p.id, mark,
                        f"convergence {captured_pct*100:.0f}%"
                    )
                    exits += 1
                    continue

        # 4. Stop loss
        from config import EDGE_STOP_LOSS
        if entry > 0:
            try:
                mark = await clob.get_price(p.token_id, "SELL") or p.entry_price
            except Exception:
                mark = p.entry_price
            if mark < entry - EDGE_STOP_LOSS:
                await positions.force_close(p.id, mark, f"stop_loss")
                exits += 1

    return exits
