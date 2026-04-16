"""
arber.py — Engine 3: Poly Arber
Scans Polymarket for internal arbitrage opportunities.

Two types:
1. Binary arb: YES + NO < $1.00 → buy both, guaranteed profit
2. Multi-outcome underpricing: total probability sum ≠ 100%
   Buy underpriced outcomes when sum < 1.0

Academic research: 41% of Polymarket conditions had single-market
arbitrage, with median mispricing of 40%.
"""

import logging
from dataclasses import dataclass
import json
from typing import Optional

from clob import ClobInterface, parse_market_tokens
from positions import PositionManager
from sizing import compute_bet_size
from config import ARBER_MIN_PROFIT, ARBER_MAX_OUTCOMES, MIN_MARKET_LIQUIDITY, ARBER_MIN_BET

logger = logging.getLogger("arber")


@dataclass
class ArberSignal:
    engine: str = "arber"
    arb_type: str = ""           # "binary" or "multi"
    condition_id: str = ""
    market_question: str = ""
    sport: str = ""
    # What to buy
    buys: list = None            # list of {side, token_id, price, shares}
    total_cost: float = 0.0      # cost to buy all sides
    guaranteed_payout: float = 0.0  # $1.00 per share set
    profit_per_unit: float = 0.0 # payout - cost per share
    profit_pct: float = 0.0      # profit / cost
    bet_size: float = 0.0        # total dollars to deploy


def _check_binary_arb(parsed: dict, clob: ClobInterface) -> Optional[ArberSignal]:
    """
    Check if YES + NO prices on a binary market sum to < $1.00.
    If so, buy both = guaranteed profit.
    """
    if len(parsed["outcomes"]) != 2 or len(parsed["token_ids"]) != 2:
        return None

    # Get real-time prices (not stale Gamma API prices)
    price_0 = clob.get_price(parsed["token_ids"][0], "BUY")
    price_1 = clob.get_price(parsed["token_ids"][1], "BUY")

    if price_0 is None or price_1 is None:
        return None

    total = price_0 + price_1

    if total >= (1.0 - ARBER_MIN_PROFIT):
        return None  # No arb — prices sum to ~$1 or more

    profit_per_unit = 1.0 - total
    profit_pct = profit_per_unit / total

    return ArberSignal(
        arb_type="binary",
        condition_id=parsed["condition_id"],
        market_question=parsed["question"],
        buys=[
            {"side": parsed["outcomes"][0], "token_id": parsed["token_ids"][0], "price": price_0},
            {"side": parsed["outcomes"][1], "token_id": parsed["token_ids"][1], "price": price_1},
        ],
        total_cost=total,
        guaranteed_payout=1.0,
        profit_per_unit=profit_per_unit,
        profit_pct=profit_pct,
    )


def _check_multi_outcome_arb(event: dict, clob: ClobInterface) -> Optional[ArberSignal]:
    """
    Check multi-outcome markets (e.g. "NBA Champion" with 30 teams).
    If sum of all YES prices < $1.00 per outcome set → buy all = guaranteed profit.
    """
    markets = event.get("markets", [])
    if len(markets) < 2:
        return None

    # Multi-outcome: each sub-market is one outcome
    outcomes = []
    total_cost = 0.0

    for m in markets:
        try:
            tokens = m.get("clobTokenIds", "[]")
            tokens = json.loads(tokens) if isinstance(tokens, str) else tokens
            if not tokens:
                continue

            outcome_names = m.get("outcomes", "[]")
            outcome_names = json.loads(outcome_names) if isinstance(outcome_names, str) else outcome_names
            if not outcome_names:
                continue

            # For multi-outcome, we want the YES token (index 0) of each
            yes_token = tokens[0]
            yes_name = outcome_names[0] if outcome_names else "?"

            price = clob.get_price(yes_token, "BUY")
            if price is None or price <= 0:
                continue

            outcomes.append({
                "side": yes_name,
                "token_id": yes_token,
                "price": price,
            })
            total_cost += price

        except Exception:
            continue

    if len(outcomes) < 2 or len(outcomes) > ARBER_MAX_OUTCOMES:
        return None

    # All outcomes: exactly one will pay $1.00
    if total_cost >= (1.0 - ARBER_MIN_PROFIT):
        return None

    profit_per_unit = 1.0 - total_cost
    profit_pct = profit_per_unit / total_cost if total_cost > 0 else 0

    return ArberSignal(
        arb_type="multi",
        condition_id=event.get("id", ""),
        market_question=event.get("title", ""),
        buys=outcomes,
        total_cost=total_cost,
        guaranteed_payout=1.0,
        profit_per_unit=profit_per_unit,
        profit_pct=profit_pct,
    )


async def scan_arber(clob: ClobInterface, positions: PositionManager) -> list[ArberSignal]:
    """
    Scan all active Polymarket sports markets for arbitrage.
    Returns guaranteed-profit signals.
    """
    signals = []
    events = await clob.fetch_all_active_markets()

    for event in events:
        try:
            # Skip if already positioned
            markets = event.get("markets", [])
            if not markets:
                continue

            # Check liquidity
            liq = float(event.get("liquidity", 0) or 0)
            if liq < MIN_MARKET_LIQUIDITY:
                continue

            # Try binary arb first (most common)
            if len(markets) == 1:
                parsed = parse_market_tokens(event)
                if parsed and not positions.has_position_for(parsed["condition_id"]):
                    arb = _check_binary_arb(parsed, clob)
                    if arb:
                        # Size: how many share-sets can we buy?
                        available = compute_bet_size("arber", arb.total_cost, 0.99, positions.equity, positions)
                        if available >= ARBER_MIN_BET:
                            arb.bet_size = available
                            arb.sport = event.get("tag", "")
                            signals.append(arb)

            # Try multi-outcome arb
            elif len(markets) >= 2:
                arb = _check_multi_outcome_arb(event, clob)
                if arb:
                    available = compute_bet_size("arber", arb.total_cost, 0.99, positions.equity, positions)
                    if available >= ARBER_MIN_BET:
                        arb.bet_size = available
                        arb.sport = event.get("tag", "")
                        signals.append(arb)

        except Exception as e:
            logger.debug(f"Arber scan error: {e}")
            continue

    for s in signals:
        logger.info(
            f"🔄 ARBER: {s.market_question[:50]} | "
            f"cost={s.total_cost:.3f} profit={s.profit_pct:.1%} ${s.bet_size}"
        )

    return signals
