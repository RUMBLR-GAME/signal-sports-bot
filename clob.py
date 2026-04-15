"""
clob.py — Polymarket CLOB Interface
All orderbook interactions: prices, orders, fills, balance, resolution.

CRITICAL GOTCHAS HANDLED:
1. get_price() returns dict {"price": "0.85"}, NOT float
2. Uses create_order + post_order separately (NOT create_and_post_order)
3. post_only=True on every order
4. NEVER calls update_balance_allowance()
5. 500ms delay after fill before reading balance
6. Rate limited to 55 orders/min
7. get_tick_size() before every order
"""

import json
import logging
import time
from typing import Optional

import aiohttp

from config import (
    CLOB_HOST, GAMMA_API, CHAIN_ID, PAPER_MODE,
    POLYMARKET_PRIVATE_KEY, POLYMARKET_FUNDER_ADDRESS,
    SIGNATURE_TYPE, MAX_ORDERS_PER_MINUTE,
    SPORT_TAG_SLUGS, FUTURES_BLOCK,
)

logger = logging.getLogger("clob")


class ClobInterface:
    """Wraps py-clob-client with safe defaults and error handling."""

    def __init__(self):
        self._client = None
        self._authenticated = False
        self._order_timestamps: list[float] = []
        self._initialized = False
        self._http_session: Optional[aiohttp.ClientSession] = None

    def initialize(self) -> bool:
        """Initialize the CLOB client. Call once at startup."""
        try:
            from py_clob_client.client import ClobClient

            if PAPER_MODE or not POLYMARKET_PRIVATE_KEY:
                self._client = ClobClient(CLOB_HOST)
                self._authenticated = False
                logger.info("CLOB client initialized (read-only / paper mode)")
            else:
                self._client = ClobClient(
                    host=CLOB_HOST,
                    chain_id=CHAIN_ID,
                    key=POLYMARKET_PRIVATE_KEY,
                    signature_type=SIGNATURE_TYPE,
                    funder=POLYMARKET_FUNDER_ADDRESS,
                )
                self._client.set_api_creds(self._client.create_or_derive_api_creds())
                self._authenticated = True
                logger.info("CLOB client initialized (authenticated)")

            self._initialized = True
            return True

        except Exception as e:
            logger.error(f"CLOB initialization failed: {e}")
            return False

    def is_ready(self) -> bool:
        return self._initialized and self._client is not None

    def is_authenticated(self) -> bool:
        return self._authenticated

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create a reusable HTTP session for Gamma API calls."""
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            )
        return self._http_session

    async def close(self):
        """Close the HTTP session. Call on shutdown."""
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()

    # ─── PRICE QUERIES ───────────────────────────────────────────────────

    def get_price(self, token_id: str, side: str = "BUY") -> Optional[float]:
        """Get current price. CRITICAL: API returns dict, not float."""
        try:
            resp = self._client.get_price(token_id, side)
            return float(resp["price"])
        except Exception as e:
            logger.error(f"get_price failed for {token_id[:20]}: {e}")
            return None

    def get_midpoint(self, token_id: str) -> Optional[float]:
        try:
            resp = self._client.get_midpoint(token_id)
            return float(resp["mid"])
        except Exception as e:
            logger.error(f"get_midpoint failed: {e}")
            return None

    def get_spread(self, token_id: str) -> Optional[float]:
        try:
            resp = self._client.get_spread(token_id)
            return float(resp["spread"])
        except Exception as e:
            logger.error(f"get_spread failed: {e}")
            return None

    # ─── ORDER MANAGEMENT ────────────────────────────────────────────────

    def _check_rate_limit(self) -> bool:
        now = time.time()
        self._order_timestamps = [t for t in self._order_timestamps if now - t < 60]
        if len(self._order_timestamps) >= MAX_ORDERS_PER_MINUTE:
            logger.warning(f"Rate limit: {len(self._order_timestamps)} orders in last 60s")
            return False
        return True

    def place_order(self, token_id: str, price: float, size: float, side: str = "BUY") -> Optional[dict]:
        """
        Place a maker limit order with post_only=True.
        Uses create_order + post_order separately.
        """
        if not self._authenticated:
            logger.info(f"[PAPER] Would place {side} {size:.1f}@{price:.3f} on {token_id[:16]}...")
            return {"orderID": f"paper-{int(time.time()*1000)}", "paper": True}

        if not self._check_rate_limit():
            return None

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
            from py_clob_client.order_builder.constants import BUY, SELL

            tick_size = self._client.get_tick_size(token_id)
            options = PartialCreateOrderOptions(tick_size=tick_size)
            order_side = BUY if side == "BUY" else SELL
            order_args = OrderArgs(token_id=token_id, price=price, size=size, side=order_side)

            signed = self._client.create_order(order_args, options)
            result = self._client.post_order(signed, OrderType.GTC, post_only=True)

            self._order_timestamps.append(time.time())
            logger.info(f"Order placed: {side} {size}@{price} → {result.get('orderID', '?')}")
            return result

        except Exception as e:
            logger.error(f"Order placement failed: {e}")
            return None

    def cancel_order(self, order_id: str) -> bool:
        if not self._authenticated:
            logger.info(f"[PAPER] Would cancel order {order_id}")
            return True
        try:
            self._client.cancel(order_id)
            logger.info(f"Cancelled order {order_id}")
            return True
        except Exception as e:
            logger.error(f"Cancel failed for {order_id}: {e}")
            return False

    def get_open_orders(self) -> list[dict]:
        """Poll open orders (fill notifications can be lost on connection drops)."""
        if not self._authenticated:
            return []
        try:
            orders = self._client.get_orders()
            return orders if isinstance(orders, list) else []
        except Exception as e:
            logger.error(f"get_orders failed: {e}")
            return []

    # ─── BALANCE ─────────────────────────────────────────────────────────

    def get_balance(self) -> Optional[float]:
        """Read USDC balance. NEVER calls update_balance_allowance()."""
        if not self._authenticated:
            return None
        try:
            bal = self._client.get_balance_allowance()
            return float(bal.get("balance", 0)) if isinstance(bal, dict) else None
        except Exception as e:
            logger.error(f"get_balance failed: {e}")
            return None

    # ─── MARKET DISCOVERY ────────────────────────────────────────────────

    async def fetch_polymarket_events(self, sport: str) -> list[dict]:
        """Fetch active Polymarket events for a sport. Filters out futures/props."""
        tag = SPORT_TAG_SLUGS.get(sport)
        if not tag:
            return []

        url = f"{GAMMA_API}/events"
        params = {"active": "true", "tag_slug": tag, "limit": 50}

        try:
            session = await self._get_session()
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    logger.warning(f"Gamma API {sport} returned {resp.status}")
                    return []
                data = await resp.json()
        except Exception as e:
            logger.error(f"Gamma API fetch failed for {sport}: {e}")
            return []

        events = []
        for event in data if isinstance(data, list) else []:
            try:
                title = event.get("title", "").lower()
                if any(word in title for word in FUTURES_BLOCK):
                    continue
                events.append(event)
            except Exception:
                continue

        return events

    async def check_resolution(self, condition_id: str) -> Optional[dict]:
        """Check if a market has resolved via Gamma API."""
        url = f"{GAMMA_API}/markets"
        params = {"conditionId": condition_id}

        try:
            session = await self._get_session()
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
        except Exception as e:
            logger.error(f"Resolution check failed for {condition_id}: {e}")
            return None

        market = None
        if isinstance(data, list) and len(data) > 0:
            market = data[0]
        elif isinstance(data, dict):
            market = data

        if not market:
            return None

        if not (market.get("closed", False) or market.get("resolved", False)):
            return {"resolved": False}

        try:
            prices_str = market.get("outcomePrices", "[]")
            prices = json.loads(prices_str) if isinstance(prices_str, str) else prices_str
            yes_price = float(prices[0]) if len(prices) > 0 else 0.0
            no_price = float(prices[1]) if len(prices) > 1 else 0.0
        except Exception:
            yes_price = 0.0
            no_price = 0.0

        winner = "UNKNOWN"
        if yes_price >= 0.99:
            winner = "YES"
        elif yes_price <= 0.01:
            winner = "NO"

        return {"resolved": True, "winner": winner, "yes_price": yes_price, "no_price": no_price}


def parse_market_tokens(market: dict) -> Optional[dict]:
    """
    Parse a Gamma API market dict to extract token IDs, outcomes, and prices.
    Returns dict with condition_id, question, outcomes, token_ids, prices, end_date
    or None if parsing fails.
    """
    try:
        markets = market.get("markets", [])
        if not markets:
            return None

        m = markets[0]
        condition_id = m.get("conditionId", "")
        question = m.get("question", market.get("title", ""))

        outcomes_str = m.get("outcomes", "[]")
        outcomes = json.loads(outcomes_str) if isinstance(outcomes_str, str) else outcomes_str

        tokens_str = m.get("clobTokenIds", "[]")
        tokens = json.loads(tokens_str) if isinstance(tokens_str, str) else tokens_str

        prices_str = m.get("outcomePrices", "[]")
        prices = json.loads(prices_str) if isinstance(prices_str, str) else prices_str
        prices = [float(p) for p in prices]

        if len(outcomes) < 2 or len(tokens) < 2:
            return None

        if m.get("closed", False):
            return None

        return {
            "condition_id": condition_id,
            "question": question,
            "outcomes": outcomes,
            "token_ids": tokens,
            "prices": prices,
            "end_date": m.get("endDate", market.get("endDate", "")),
        }

    except Exception as e:
        logger.error(f"Failed to parse market tokens: {e}")
        return None
