"""
sizing.py — Kelly Sizing with Sport-Specific Risk Multipliers
Soccer/NHL get full Kelly. NBA/NCAAB get half Kelly.
Capital allocation limits per engine.
"""

import logging
from config import (
    KELLY_FRACTION, MAX_POSITION_PCT, MIN_TRADE_SIZE,
    MAX_TOTAL_EXPOSURE, MAX_EDGE_EXPOSURE, MAX_ARBER_EXPOSURE,
    SPORT_RISK_MULTIPLIER, SLEEPING_LION, EDGE_SIZE_LADDER,
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
    f = min(f, MAX_POSITION_PCT)
    return f


def compute_bet_size(
    engine: str,
    price: float,
    win_prob: float,
    equity: float,
    positions: "PositionManager",
    sport: str = "",
    edge: float = 0.0,
) -> float:
    """
    Compute dollar bet size with sport-specific risk adjustment.

    Harvest: applies SPORT_RISK_MULTIPLIER (soccer=1.0, NBA=0.5)
    Edge Finder: applies SLEEPING_LION (obscure leagues=1.5×) + EDGE_SIZE_LADDER (bigger edge=bigger bet)
    Arber: standard Kelly
    """
    if equity <= 0:
        return 0.0

    frac = kelly_fraction(price, win_prob)
    if frac <= 0:
        return 0.0

    # Sport-specific risk multiplier (Harvest only)
    if engine == "harvest" and sport:
        multiplier = SPORT_RISK_MULTIPLIER.get(sport, 0.8)
        frac *= multiplier

    # Sleeping Lion + Edge Ladder (Edge Finder only)
    if engine == "edge" and sport:
        # Low-attention markets get bigger bets (staler pricing = more reliable edge)
        lion = SLEEPING_LION.get(sport, 1.0)
        frac *= lion

        # Bigger edges get bigger bets (laddered)
        if edge > 0:
            for min_edge, mult in EDGE_SIZE_LADDER:
                if edge >= min_edge:
                    frac *= mult
                    break

    size = frac * equity

    # Engine capital limits
    if not _check_engine_cap(engine, size, positions):
        available = _available_for_engine(engine, equity, positions)
        if available < MIN_TRADE_SIZE:
            return 0.0
        size = min(size, available)

    # Total exposure limit — hard cap at STARTING_BANKROLL
    from config import STARTING_BANKROLL
    total_open = positions.open_cost
    max_open = STARTING_BANKROLL * MAX_TOTAL_EXPOSURE
    if total_open + size > max_open:
        remaining = max_open - total_open
        if remaining < MIN_TRADE_SIZE:
            return 0.0
        size = min(size, remaining)

    # Minimum floor
    if size < MIN_TRADE_SIZE:
        if equity >= MIN_TRADE_SIZE * 2:
            size = MIN_TRADE_SIZE
        else:
            return 0.0

    return round(size, 2)


def _check_engine_cap(engine: str, amount: float, positions) -> bool:
    total_equity = positions.equity + positions.open_cost
    used = sum(p.cost for p in positions.get_open_positions() if p.engine == engine)
    cap = _engine_limit(engine) * total_equity
    return used + amount <= cap


def _available_for_engine(engine: str, equity: float, positions) -> float:
    total_equity = equity + positions.open_cost
    cap = _engine_limit(engine) * total_equity
    used = sum(p.cost for p in positions.get_open_positions() if p.engine == engine)
    return max(cap - used, 0.0)


def _engine_limit(engine: str) -> float:
    return {"harvest": 1.0, "edge": MAX_EDGE_EXPOSURE, "arber": MAX_ARBER_EXPOSURE}.get(engine, 0.10)
