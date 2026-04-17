"""
sizing.py — Kelly Sizing (v18)

CRITICAL FIX from v17: MAX_TOTAL_EXPOSURE now scales with CURRENT equity
(was fixed at STARTING_BANKROLL). This was the reason the bot couldn't compound.

Also adds:
  • Drawdown governor — auto-halves Kelly after 15% drawdown from peak
  • Correlation caps — per-sport and per-time-window exposure limits
  • Min-trade floor is stricter — no forcing sub-Kelly bets to $5
"""
import logging
from typing import Tuple, Optional
from config import (
    KELLY_FRACTION, MAX_POSITION_PCT, MIN_TRADE_SIZE,
    MAX_TOTAL_EXPOSURE, MAX_EDGE_EXPOSURE,
    MAX_EXPOSURE_PER_SPORT, MAX_EXPOSURE_PER_WINDOW,
    SPORT_RISK_MULTIPLIER, SLEEPING_LION, EDGE_SIZE_LADDER,
    DRAWDOWN_THRESHOLD, DRAWDOWN_KELLY_MULT,
)

logger = logging.getLogger("sizing")


def kelly_fraction(price: float, win_prob: float) -> float:
    """Raw quarter-Kelly fraction for a binary market."""
    if price <= 0 or price >= 1 or win_prob <= 0 or win_prob >= 1:
        return 0.0
    b = (1.0 - price) / price
    q = 1.0 - win_prob
    f = (win_prob * b - q) / b
    if f <= 0:
        return 0.0
    f *= KELLY_FRACTION
    return min(f, MAX_POSITION_PCT)


def drawdown_mult(equity: float, peak_equity: float) -> float:
    if peak_equity <= 0:
        return 1.0
    dd = max(0.0, (peak_equity - equity) / peak_equity)
    return DRAWDOWN_KELLY_MULT if dd >= DRAWDOWN_THRESHOLD else 1.0


def compute_bet_size(
    engine: str,
    price: float,
    win_prob: float,
    equity: float,
    positions,                          # PositionManager
    sport: str = "",
    edge: float = 0.0,
    game_start_ts: Optional[float] = None,
) -> Tuple[float, str]:
    """
    Returns (dollar_size, reason). Size=0 means skip.
    Reason string is human-readable and logged for diagnostics.
    """
    if equity <= 0:
        return 0.0, "equity<=0"

    frac = kelly_fraction(price, win_prob)
    if frac <= 0:
        return 0.0, "kelly<=0"

    sport_mult = 1.0
    lion_mult = 1.0
    ladder_mult = 1.0

    if engine == "harvest":
        sport_mult = SPORT_RISK_MULTIPLIER.get(sport, 0.7)
        frac *= sport_mult
    elif engine == "edge":
        lion_mult = SLEEPING_LION.get(sport, 1.0)
        frac *= lion_mult
        if edge > 0:
            for min_e, mult in EDGE_SIZE_LADDER:
                if edge >= min_e:
                    ladder_mult = mult
                    frac *= mult
                    break

    # Drawdown governor (protects compounding after a bad run)
    peak = getattr(positions, "peak_equity", equity)
    dd_mult = drawdown_mult(equity, peak)
    frac *= dd_mult

    raw_size = frac * equity

    # Total exposure cap — FIXED to use current equity
    max_total = equity * MAX_TOTAL_EXPOSURE
    total_open = positions.open_cost
    total_room = max_total - total_open
    if total_room <= 0:
        return 0.0, f"total exposure cap (${total_open:.0f}/${max_total:.0f})"
    size = min(raw_size, total_room)

    # Engine-specific cap
    if engine == "edge":
        edge_cap = equity * MAX_EDGE_EXPOSURE
        edge_open = positions.deployed_by_engine("edge")
        edge_room = edge_cap - edge_open
        if edge_room <= 0:
            return 0.0, f"edge engine cap (${edge_open:.0f}/${edge_cap:.0f})"
        size = min(size, edge_room)

    # Per-sport cap
    if sport:
        sport_cap = equity * MAX_EXPOSURE_PER_SPORT
        sport_open = positions.deployed_by_sport(sport)
        sport_room = sport_cap - sport_open
        if sport_room <= 0:
            return 0.0, f"sport cap {sport} (${sport_open:.0f}/${sport_cap:.0f})"
        size = min(size, sport_room)

    # Per-time-window cap (only meaningful for Edge, pre-game)
    if game_start_ts is not None:
        window_cap = equity * MAX_EXPOSURE_PER_WINDOW
        window_open = positions.deployed_in_window(game_start_ts)
        window_room = window_cap - window_open
        if window_room <= 0:
            return 0.0, f"time-window cap (${window_open:.0f}/${window_cap:.0f})"
        size = min(size, window_room)

    if size < MIN_TRADE_SIZE:
        return 0.0, f"size ${size:.2f} < min ${MIN_TRADE_SIZE}"

    reason = (
        f"kelly={frac/dd_mult/ladder_mult/lion_mult/sport_mult if frac else 0:.3f} "
        f"sport={sport_mult:.2f} lion={lion_mult:.2f} ladder={ladder_mult:.2f} "
        f"dd={dd_mult:.2f} → ${size:.2f}"
    )
    return round(size, 2), reason
