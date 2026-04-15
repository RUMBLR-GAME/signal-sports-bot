"""
polymarket_prices.py — Real Polymarket CLOB price fetcher
Queries actual orderbook prices via py-clob-client.
NO wallet/auth needed for price reads.

Drop this file alongside crypto.py in the bot root.
Add `py-clob-client==0.34.6` to requirements.txt.
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


def _build_slug(asset: str, window_min: int) -> str:
    """Build the Polymarket event slug for a crypto up/down market.

    asset: 'BTC' or 'ETH' (uppercased from crypto.py — we lowercase here)
    window_min: 5 or 15
    """
    now = int(time.time())
    period = window_min * 60
    window_ts = now - (now % period)
    return f"{asset.lower()}-updown-{window_min}m-{window_ts}"


def _build_slug_from_ts(asset: str, window_min: int, window_ts: int) -> str:
    """Build slug using a specific window timestamp (for callers that already computed it)."""
    return f"{asset.lower()}-updown-{window_min}m-{window_ts}"


def _get_token_ids(slug: str) -> dict | None:
    """Fetch token IDs from Gamma API for a given slug.
    Returns {'yes': token_id, 'no': token_id} or None on failure.
    """
    # Check cache
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

        # Map outcomes to token IDs
        result = {}
        for i, outcome in enumerate(outcomes):
            key = outcome.strip().lower()
            if key in ("yes", "up"):
                result["yes"] = token_ids[i]
            elif key in ("no", "down"):
                result["no"] = token_ids[i]

        # Fallback if outcomes don't match expected names
        if "yes" not in result:
            result["yes"] = token_ids[0]
        if "no" not in result:
            result["no"] = token_ids[1]

        _token_cache[slug] = (time.time(), result)
        logger.info(
            f"Resolved tokens for {slug}: YES={result['yes'][:12]}… NO={result['no'][:12]}…"
        )
        return result

    except Exception as e:
        logger.error(f"Gamma API error for slug {slug}: {e}")
        return None


def get_real_price(slug: str, direction: str) -> dict | None:
    """Get real Polymarket price for a crypto up/down market.

    Args:
        slug: Full Polymarket slug, e.g. "btc-updown-5m-1712345678"
              (matches what crypto.py already builds)
        direction: 'up' or 'down'

    Returns dict with price data or None on failure:
        {
            'slug': str,
            'direction': str,
            'token_id': str,
            'buy_price': float,    # what we'd pay to buy
            'sell_price': float,   # what we'd get selling
            'midpoint': float,     # midpoint price
            'spread': float,       # bid-ask spread
            'timestamp': float,
        }
    """
    tokens = _get_token_ids(slug)
    if not tokens:
        return None

    # Pick the right token based on direction
    token_key = "yes" if direction == "up" else "no"
    token_id = tokens.get(token_key)

    if not token_id:
        logger.error(f"No {token_key} token for {slug}")
        return None

    try:
        # ── Get real orderbook prices from CLOB ──
        # CLOB API returns dicts: {"price": "0.85"}, {"mid": "0.83"}, {"spread": "0.02"}
        price_resp = _clob.get_price(token_id, "BUY")
        buy_price = float(price_resp["price"]) if isinstance(price_resp, dict) else float(price_resp)

        sell_resp = _clob.get_price(token_id, "SELL")
        sell_price = float(sell_resp["price"]) if isinstance(sell_resp, dict) else float(sell_resp)

        mid_resp = _clob.get_midpoint(token_id)
        midpoint = float(mid_resp["mid"]) if isinstance(mid_resp, dict) else float(mid_resp)

        spread_resp = _clob.get_spread(token_id)
        spread = float(spread_resp["spread"]) if isinstance(spread_resp, dict) else float(spread_resp)

        result = {
            "slug": slug,
            "direction": direction,
            "token_id": token_id,
            "buy_price": buy_price,
            "sell_price": sell_price,
            "midpoint": midpoint,
            "spread": spread,
            "timestamp": time.time(),
        }

        logger.info(
            f"REAL PRICE {slug} {direction}: "
            f"buy={buy_price:.4f} sell={sell_price:.4f} mid={midpoint:.4f} spread={spread:.4f}"
        )
        return result

    except Exception as e:
        logger.error(f"CLOB price error for {slug} ({direction}): {e}")
        return None
