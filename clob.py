"""
clob.py — Polymarket CLOB Interface
Prices, orders, fills, balance, resolution, market discovery.
All CLOB gotchas handled (see v3 handoff for full list).
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
    def __init__(self):
        self._client = None
        self._authenticated = False
        self._order_timestamps: list[float] = []
        self._initialized = False
        self._session: Optional[aiohttp.ClientSession] = None

    def initialize(self) -> bool:
        try:
            from py_clob_client.client import ClobClient
            if PAPER_MODE or not POLYMARKET_PRIVATE_KEY:
                self._client = ClobClient(CLOB_HOST)
                self._authenticated = False
                logger.info("CLOB: read-only (paper mode)")
            else:
                self._client = ClobClient(
                    host=CLOB_HOST, chain_id=CHAIN_ID,
                    key=POLYMARKET_PRIVATE_KEY,
                    signature_type=SIGNATURE_TYPE,
                    funder=POLYMARKET_FUNDER_ADDRESS,
                )
                self._client.set_api_creds(self._client.create_or_derive_api_creds())
                self._authenticated = True
                logger.info("CLOB: authenticated")
            self._initialized = True
            return True
        except Exception as e:
            logger.error(f"CLOB init failed: {e}")
            return False

    def is_ready(self): return self._initialized
    def is_authenticated(self): return self._authenticated

    async def _get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # ─── PRICES ──────────────────────────────────────────────────────────

    def get_price(self, token_id: str, side: str = "BUY") -> Optional[float]:
        try:
            resp = self._client.get_price(token_id, side)
            return float(resp["price"])
        except Exception as e:
            logger.error(f"get_price failed: {e}")
            return None

    def get_midpoint(self, token_id: str) -> Optional[float]:
        try:
            return float(self._client.get_midpoint(token_id)["mid"])
        except Exception:
            return None

    # ─── ORDERS ──────────────────────────────────────────────────────────

    def _check_rate_limit(self) -> bool:
        now = time.time()
        self._order_timestamps = [t for t in self._order_timestamps if now - t < 60]
        return len(self._order_timestamps) < MAX_ORDERS_PER_MINUTE

    def place_order(self, token_id: str, price: float, size: float, side: str = "BUY") -> Optional[dict]:
        if not self._authenticated:
            logger.info(f"[PAPER] {side} {size:.1f}@{price:.3f}")
            return {"orderID": f"paper-{int(time.time()*1000)}", "paper": True}

        if not self._check_rate_limit():
            return None

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
            from py_clob_client.order_builder.constants import BUY, SELL

            tick_size = self._client.get_tick_size(token_id)
            options = PartialCreateOrderOptions(tick_size=tick_size)
            order_args = OrderArgs(
                token_id=token_id, price=price, size=size,
                side=BUY if side == "BUY" else SELL,
            )
            signed = self._client.create_order(order_args, options)
            result = self._client.post_order(signed, OrderType.GTC, post_only=True)
            self._order_timestamps.append(time.time())
            logger.info(f"Order: {side} {size}@{price} → {result.get('orderID','?')}")
            return result
        except Exception as e:
            logger.error(f"Order failed: {e}")
            return None

    def cancel_order(self, order_id: str) -> bool:
        if not self._authenticated:
            return True
        try:
            self._client.cancel(order_id)
            return True
        except Exception as e:
            logger.error(f"Cancel failed: {e}")
            return False

    def get_open_orders(self) -> list[dict]:
        if not self._authenticated:
            return []
        try:
            orders = self._client.get_orders()
            return orders if isinstance(orders, list) else []
        except Exception:
            return []

    def get_balance(self) -> Optional[float]:
        if not self._authenticated:
            return None
        try:
            bal = self._client.get_balance_allowance()
            return float(bal.get("balance", 0)) if isinstance(bal, dict) else None
        except Exception:
            return None

    # ─── MARKET DISCOVERY ────────────────────────────────────────────────

    async def fetch_polymarket_events(self, sport: str) -> list[dict]:
        tag = SPORT_TAG_SLUGS.get(sport)
        if not tag:
            return []
        try:
            session = await self._get_session()
            async with session.get(f"{GAMMA_API}/events", params={"active": "true", "tag_slug": tag, "limit": 50}) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
        except Exception as e:
            logger.error(f"Gamma API {sport}: {e}")
            return []

        return [
            ev for ev in (data if isinstance(data, list) else [])
            if not any(w in ev.get("title", "").lower() for w in FUTURES_BLOCK)
        ]

    async def fetch_all_active_markets(self) -> list[dict]:
        """Fetch ALL active Polymarket markets (for Poly Arber). No sport filter."""
        all_markets = []
        try:
            session = await self._get_session()
            for tag in SPORT_TAG_SLUGS.values():
                try:
                    async with session.get(f"{GAMMA_API}/events", params={"active": "true", "tag_slug": tag, "limit": 50}) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if isinstance(data, list):
                                all_markets.extend(data)
                except Exception:
                    continue
        except Exception as e:
            logger.error(f"fetch_all_active_markets: {e}")

        return all_markets

    async def check_resolution(self, condition_id: str) -> Optional[dict]:
        try:
            session = await self._get_session()
            async with session.get(f"{GAMMA_API}/markets", params={"conditionId": condition_id}) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
        except Exception as e:
            logger.error(f"Resolution check: {e}")
            return None

        market = data[0] if isinstance(data, list) and data else data if isinstance(data, dict) else None
        if not market:
            return None

        if not (market.get("closed") or market.get("resolved")):
            return {"resolved": False}

        try:
            prices = market.get("outcomePrices", "[]")
            prices = json.loads(prices) if isinstance(prices, str) else prices
            yp = float(prices[0]) if len(prices) > 0 else 0.0
        except Exception:
            yp = 0.0

        winner = "YES" if yp >= 0.99 else "NO" if yp <= 0.01 else "UNKNOWN"
        return {"resolved": True, "winner": winner, "yes_price": yp}


def parse_market_tokens(market: dict) -> Optional[dict]:
    """Parse Gamma API event → token IDs, outcomes, prices."""
    try:
        markets = market.get("markets", [])
        if not markets:
            return None
        m = markets[0]
        if m.get("closed"):
            return None

        outcomes = m.get("outcomes", "[]")
        outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
        tokens = m.get("clobTokenIds", "[]")
        tokens = json.loads(tokens) if isinstance(tokens, str) else tokens
        prices = m.get("outcomePrices", "[]")
        prices = json.loads(prices) if isinstance(prices, str) else prices
        prices = [float(p) for p in prices]

        if len(outcomes) < 2 or len(tokens) < 2:
            return None

        return {
            "condition_id": m.get("conditionId", ""),
            "question": m.get("question", market.get("title", "")),
            "outcomes": outcomes,
            "token_ids": tokens,
            "prices": prices,
            "end_date": m.get("endDate", ""),
            "volume": float(market.get("volume", 0) or 0),
            "liquidity": float(market.get("liquidity", 0) or 0),
        }
    except Exception as e:
        logger.error(f"parse_market_tokens: {e}")
        return None
