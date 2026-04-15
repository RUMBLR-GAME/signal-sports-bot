"""
polymarket_prices.py — Real Polymarket CLOB price fetcher v2

Queries actual orderbook prices via py-clob-client.
NO wallet/auth needed for price reads.

Features:
  - Crypto up/down market prices (slug-based)
  - Sports market prices (token_id-based, for harvest)
  - Pair arbitrage detection (YES+NO < threshold)
"""

import time
import logging
import requests
from py_clob_client.client import ClobClient

logger = logging.getLogger("polymarket_prices")

# ── CLOB client (read-only, no auth) ───────────────────────────────
CLOB_HOST = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

_clob = ClobClient(CLOB_HOST)

# Cache token IDs for 60s to avoid hammering Gamma
_token_cache: dict[str, tuple[float, dict]] = {}
TOKEN_CACHE_TTL = 60


def _get_token_ids(slug: str) -> dict | None:
    """Fetch token IDs from Gamma API for a given slug.
    Returns {'yes': token_id, 'no': token_id} or None on failure.
    """
    if slug in _token_cache:
        cached_at, data = _token_cache[slug]
        if time.time() - cached_at < TOKEN_CACHE_TTL:
            return data

    try:
        resp = requests.get(
            f"{GAMMA_API}/events",
            params={"slug": slug},
            timeout=5,
        )
        resp.raise_for_status()
        events = resp.json()

        if not events or not isinstance(events, list) or len(events) == 0:
            logger.warning(f"No events found for slug: {slug}")
            return None

        event = events[0]
        markets = event.get("markets", [])
        if not markets:
            logger.warning(f"No markets in event for slug: {slug}")
            return None

        market = markets[0]
        token_ids = market.get("clobTokenIds", [])
        outcomes = market.get("outcomes", [])

        if len(token_ids) < 2:
            logger.warning(f"Not enough token IDs for slug: {slug}")
            return None

        result = {}
        for i, outcome in enumerate(outcomes):
            key = outcome.strip().lower()
            if key in ("yes", "up"):
                result["yes"] = token_ids[i]
            elif key in ("no", "down"):
                result["no"] = token_ids[i]

        if "yes" not in result:
            result["yes"] = token_ids[0]
        if "no" not in result:
            result["no"] = token_ids[1]

        _token_cache[slug] = (time.time(), result)
        return result

    except Exception as e:
        logger.error(f"Gamma API error for slug {slug}: {e}")
        return None


def _query_clob_price(token_id: str) -> dict | None:
    """Query CLOB for a single token's prices. Returns parsed dict or None."""
    try:
        price_resp = _clob.get_price(token_id, "BUY")
        buy = float(price_resp["price"]) if isinstance(price_resp, dict) else float(price_resp)

        sell_resp = _clob.get_price(token_id, "SELL")
        sell = float(sell_resp["price"]) if isinstance(sell_resp, dict) else float(sell_resp)

        mid_resp = _clob.get_midpoint(token_id)
        mid = float(mid_resp["mid"]) if isinstance(mid_resp, dict) else float(mid_resp)

        spread_resp = _clob.get_spread(token_id)
        spread = float(spread_resp["spread"]) if isinstance(spread_resp, dict) else float(spread_resp)

        return {"buy_price": buy, "sell_price": sell, "midpoint": mid, "spread": spread}
    except Exception as e:
        logger.error(f"CLOB query error for token {token_id[:16]}…: {e}")
        return None


# ── PUBLIC API ─────────────────────────────────────────────────────

def get_real_price(slug: str, direction: str) -> dict | None:
    """Get real Polymarket price for a crypto up/down market.

    Args:
        slug: Full Polymarket slug, e.g. "btc-updown-5m-1712345678"
        direction: 'up' or 'down'

    Returns dict or None:
        { slug, direction, token_id, buy_price, sell_price, midpoint, spread, timestamp }
    """
    tokens = _get_token_ids(slug)
    if not tokens:
        return None

    token_key = "yes" if direction == "up" else "no"
    token_id = tokens.get(token_key)
    if not token_id:
        logger.error(f"No {token_key} token for {slug}")
        return None

    data = _query_clob_price(token_id)
    if not data:
        return None

    result = {
        "slug": slug,
        "direction": direction,
        "token_id": token_id,
        **data,
        "timestamp": time.time(),
    }

    logger.info(
        f"REAL PRICE {slug} {direction}: "
        f"buy={data['buy_price']:.4f} sell={data['sell_price']:.4f} "
        f"mid={data['midpoint']:.4f} spread={data['spread']:.4f}"
    )
    return result


def get_token_price(token_id: str) -> dict | None:
    """Get real CLOB price for any token by ID (used by harvest for sports markets).

    Returns dict or None:
        { token_id, buy_price, sell_price, midpoint, spread, timestamp }
    """
    if not token_id:
        return None

    data = _query_clob_price(token_id)
    if not data:
        return None

    return {"token_id": token_id, **data, "timestamp": time.time()}


def check_arb(slug: str) -> dict | None:
    """Check if a crypto market has an arbitrage opportunity.
    Buy both YES and NO when combined cost < $0.97 for risk-free profit.

    Returns dict or None:
        { slug, yes_price, no_price, combined, profit_per_share,
          yes_token, no_token, timestamp }
    """
    tokens = _get_token_ids(slug)
    if not tokens:
        return None

    yes_id = tokens.get("yes")
    no_id = tokens.get("no")
    if not yes_id or not no_id:
        return None

    yes_data = _query_clob_price(yes_id)
    no_data = _query_clob_price(no_id)
    if not yes_data or not no_data:
        return None

    combined = yes_data["buy_price"] + no_data["buy_price"]

    if combined >= 1.0:
        return None  # No arb — would lose money

    profit = 1.0 - combined  # Per share pair

    result = {
        "slug": slug,
        "yes_price": yes_data["buy_price"],
        "no_price": no_data["buy_price"],
        "yes_spread": yes_data["spread"],
        "no_spread": no_data["spread"],
        "combined": round(combined, 4),
        "profit_per_share": round(profit, 4),
        "yes_token": yes_id,
        "no_token": no_id,
        "timestamp": time.time(),
    }

    logger.info(
        f"ARB CHECK {slug}: YES={yes_data['buy_price']:.4f} + NO={no_data['buy_price']:.4f} "
        f"= {combined:.4f} → profit={profit:.4f}/share"
    )
    return result
