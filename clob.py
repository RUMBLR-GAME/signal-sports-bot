"""
clob.py — Polymarket CLOB & Gamma Interface (v18)

v17 issues fixed:
  • get_price() and place_order() were sync but called from async contexts,
    blocking the event loop. Now wrapped with asyncio.to_thread().
  • _poly_diag was attached to instance via attribute — now a proper field.
  • Added orderbook depth check so we don't place orders into thin liquidity.
  • Added verify_series_ids() to cross-check config against /sports on boot.
  • Added resolve_market() that queries by conditionId with cleaner logic.
"""
import asyncio
import json
import logging
import time
from typing import Optional, Dict, List
import aiohttp

from config import (
    CLOB_HOST, GAMMA_API, CHAIN_ID, PAPER_MODE,
    POLYMARKET_PRIVATE_KEY, POLYMARKET_FUNDER_ADDRESS,
    SIGNATURE_TYPE, MAX_ORDERS_PER_MINUTE,
    FUTURES_BLOCK,
    POLY_SERIES_IDS, POLY_GAMES_TAG_ID,
)

logger = logging.getLogger("clob")


class ClobInterface:
    def __init__(self):
        self._client = None
        self._authenticated = False
        self._order_timestamps: List[float] = []
        self._initialized = False
        self._session: Optional[aiohttp.ClientSession] = None
        # Diagnostics exposed via dashboard
        self.poly_diag: Dict[str, dict] = {}
        self.series_verified: Dict[str, bool] = {}
        # TTL cache for Gamma /events per sport
        self._events_cache: Dict[str, tuple] = {}

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

    def is_ready(self) -> bool:
        return self._initialized

    def is_authenticated(self) -> bool:
        return self._authenticated

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                connector=aiohttp.TCPConnector(limit=50, limit_per_host=20),
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # ─── Async price wrappers ──────────────────────────────────────────
    async def get_price(self, token_id: str, side: str = "BUY") -> Optional[float]:
        if not self._client:
            return None
        try:
            resp = await asyncio.to_thread(self._client.get_price, token_id, side)
            return float(resp["price"])
        except Exception as e:
            logger.debug(f"get_price {token_id[-8:]} {side}: {e}")
            return None

    async def get_midpoint(self, token_id: str) -> Optional[float]:
        if not self._client:
            return None
        try:
            resp = await asyncio.to_thread(self._client.get_midpoint, token_id)
            return float(resp["mid"])
        except Exception:
            return None

    async def get_orderbook(self, token_id: str) -> Optional[dict]:
        """Fetch orderbook with bid/ask depth."""
        if not self._client:
            return None
        try:
            resp = await asyncio.to_thread(self._client.get_order_book, token_id)
            return resp
        except Exception:
            return None

    async def depth_at_price(self, token_id: str, price: float, side: str = "BUY") -> float:
        """
        Return total size available at or better than `price` on the given side.
        side=BUY means we're buying → look at asks ≤ price.
        side=SELL means we're selling → look at bids ≥ price.
        """
        book = await self.get_orderbook(token_id)
        if not book:
            return 0.0
        levels = book.get("asks" if side == "BUY" else "bids", []) or []
        total = 0.0
        for lv in levels:
            try:
                p = float(lv.get("price"))
                s = float(lv.get("size"))
            except (TypeError, ValueError):
                continue
            if side == "BUY" and p <= price:
                total += s
            elif side == "SELL" and p >= price:
                total += s
        return total

    # ─── Orders ────────────────────────────────────────────────────────
    def _check_rate_limit(self) -> bool:
        now = time.time()
        self._order_timestamps = [t for t in self._order_timestamps if now - t < 60]
        return len(self._order_timestamps) < MAX_ORDERS_PER_MINUTE

    async def place_order(
        self, token_id: str, price: float, size: float, side: str = "BUY"
    ) -> Optional[dict]:
        if not self._authenticated:
            logger.info(f"[PAPER] {side} {size:.1f}@{price:.3f}")
            return {"orderID": f"paper-{int(time.time()*1000)}", "paper": True}

        if not self._check_rate_limit():
            logger.warning("rate limit")
            return None

        def _place():
            from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
            from py_clob_client.order_builder.constants import BUY, SELL
            tick_size = self._client.get_tick_size(token_id)
            options = PartialCreateOrderOptions(tick_size=tick_size)
            # Round price to tick
            p = round(price / tick_size) * tick_size
            order_args = OrderArgs(
                token_id=token_id, price=p, size=size,
                side=BUY if side == "BUY" else SELL,
            )
            signed = self._client.create_order(order_args, options)
            return self._client.post_order(signed, OrderType.GTC, post_only=True)

        try:
            result = await asyncio.to_thread(_place)
            self._order_timestamps.append(time.time())
            logger.info(f"Order: {side} {size}@{price:.3f} → {result.get('orderID','?')}")
            return result
        except Exception as e:
            logger.error(f"place_order: {e}")
            return None

    async def cancel_order(self, order_id: str) -> bool:
        if not self._authenticated:
            return True
        try:
            await asyncio.to_thread(self._client.cancel, order_id)
            return True
        except Exception as e:
            logger.debug(f"cancel: {e}")
            return False

    async def get_open_orders(self) -> list:
        if not self._authenticated:
            return []
        try:
            orders = await asyncio.to_thread(self._client.get_orders)
            return orders if isinstance(orders, list) else []
        except Exception:
            return []

    # ─── Market discovery ──────────────────────────────────────────────
    async def fetch_polymarket_events(self, sport: str, max_age_sec: float = 45) -> list:
        """
        Fetch current game markets using the official pattern:
          /events?series_id={id}&tag_id=100639&active=true&closed=false

        TTL-cached in-memory to avoid hitting Gamma repeatedly within a scan cycle.
        Pass max_age_sec=0 to force refresh.
        """
        series_id = POLY_SERIES_IDS.get(sport)
        if not series_id:
            return []

        # TTL cache check
        now = time.time()
        cached = self._events_cache.get(sport)
        if cached and max_age_sec > 0 and (now - cached[0]) < max_age_sec:
            return cached[1]

        try:
            session = await self._get_session()
            params = {
                "series_id": series_id,
                "tag_id": POLY_GAMES_TAG_ID,
                "active": "true",
                "closed": "false",
                "archived": "false",
                "limit": 100,
                "order": "startTime",
                "ascending": "true",
            }
            async with session.get(f"{GAMMA_API}/events", params=params, timeout=8) as resp:
                if resp.status != 200:
                    logger.debug(f"Gamma {sport} (series {series_id}): HTTP {resp.status}")
                    self.poly_diag[sport] = {
                        "tag": f"series={series_id}", "raw": 0, "filtered": 0,
                        "sample_titles": [], "http_status": resp.status,
                    }
                    return cached[1] if cached else []
                data = await resp.json()
        except Exception as e:
            logger.debug(f"Gamma {sport}: {e}")
            return cached[1] if cached else []

        raw = data if isinstance(data, list) else []
        filtered = [
            ev for ev in raw
            if not any(w in ev.get("title", "").lower() for w in FUTURES_BLOCK)
        ]
        self.poly_diag[sport] = {
            "tag": f"series={series_id}", "raw": len(raw), "filtered": len(filtered),
            "sample_titles": [ev.get("title", "") for ev in filtered[:5]],
        }
        self._events_cache[sport] = (now, filtered)
        return filtered

    async def check_resolution(self, condition_id: str) -> Optional[dict]:
        """Check if a market has resolved. Returns {resolved, winner, yes_price}."""
        try:
            session = await self._get_session()
            async with session.get(
                f"{GAMMA_API}/markets",
                params={"conditionId": condition_id},
                timeout=8,
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
        except Exception:
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

    async def verify_series_ids(self) -> Dict[str, bool]:
        """
        Cross-check configured series IDs against Polymarket's /sports endpoint.
        Logs mismatches. Results exposed via dashboard.
        """
        try:
            session = await self._get_session()
            async with session.get(f"{GAMMA_API}/sports", timeout=10) as resp:
                if resp.status != 200:
                    logger.warning(f"/sports returned {resp.status}")
                    return {}
                data = await resp.json()
        except Exception as e:
            logger.warning(f"verify_series_ids: {e}")
            return {}

        live_ids = set()
        if isinstance(data, list):
            for s in data:
                sid = s.get("id") or s.get("seriesId")
                if sid is not None:
                    live_ids.add(str(sid))

        verified = {}
        for sport, sid in POLY_SERIES_IDS.items():
            ok = str(sid) in live_ids
            verified[sport] = ok
            if not ok:
                logger.warning(f"Configured series_id {sid} for {sport} NOT FOUND in live /sports")
        self.series_verified = verified
        return verified


def parse_market_tokens(market: dict) -> Optional[dict]:
    """Parse a Gamma event → dict with outcomes, token_ids, prices."""
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

        # Last trade time (for staleness check)
        last_trade = m.get("lastTradeTime") or m.get("last_trade_time")

        return {
            "condition_id": m.get("conditionId", ""),
            "question": m.get("question", market.get("title", "")),
            "outcomes": outcomes,
            "token_ids": tokens,
            "prices": prices,
            "end_date": m.get("endDate", ""),
            "volume": float(market.get("volume", 0) or 0),
            "liquidity": float(market.get("liquidity", 0) or 0),
            "last_trade_time": last_trade,
        }
    except Exception as e:
        logger.debug(f"parse_market_tokens: {e}")
        return None
