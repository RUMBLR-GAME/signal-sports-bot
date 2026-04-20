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

    async def get_price_http(self, token_id: str, side: str = "BUY") -> Optional[float]:
        """
        Pure-HTTP price fetch — works without py_clob_client initialized.
        Used as a live-price fallback for dashboard enrichment in paper mode.
        """
        try:
            session = await self._get_session()
            url = f"{CLOB_HOST}/price"
            params = {"token_id": token_id, "side": side}
            async with session.get(url, params=params, timeout=5) as r:
                if r.status != 200:
                    return None
                data = await r.json()
                if "price" in data:
                    return float(data["price"])
                return None
        except Exception as e:
            logger.debug(f"get_price_http {token_id[-8:]} {side}: {e}")
            return None

    async def get_midpoint_http(self, token_id: str) -> Optional[float]:
        """
        Pure-HTTP midpoint fetch — works without py_clob_client initialized.
        Uses /book endpoint to get bid+ask and validate the pair is real
        (rejects empty-book garbage like bid=1/ask=1, bid=0/ask=0,
        or wide spreads that sum inconsistently across sides).
        """
        try:
            session = await self._get_session()
            url = f"{CLOB_HOST}/book"
            params = {"token_id": token_id}
            async with session.get(url, params=params, timeout=5) as r:
                if r.status != 200:
                    return None
                data = await r.json()
                bids = data.get("bids", []) or []
                asks = data.get("asks", []) or []
                if not bids or not asks:
                    return None  # one-sided book — can't compute real midpoint
                try:
                    best_bid = float(bids[0].get("price", 0))
                    best_ask = float(asks[0].get("price", 0))
                except (TypeError, ValueError, IndexError):
                    return None
                # Reject nonsense books (flat bid=ask, degenerate ranges)
                if best_bid <= 0 or best_ask <= 0:
                    return None
                if best_bid >= best_ask:
                    return None  # crossed or flat book — not real
                if best_ask - best_bid > 0.25:
                    return None  # spread too wide (> 25¢) — illiquid, untrustworthy
                mid = (best_bid + best_ask) / 2.0
                # Edge-case: midpoints at extremes (<2¢ or >98¢) only valid
                # if the spread is tight. If spread is >5¢ AND we're near extremes,
                # the quote is too thin to trust.
                if (mid < 0.03 or mid > 0.97) and (best_ask - best_bid) > 0.05:
                    return None
                return mid
        except Exception as e:
            logger.debug(f"get_midpoint_http {token_id[-8:]}: {e}")
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

    async def place_order_fok(
        self, token_id: str, price: float, size: float, side: str = "BUY"
    ) -> Optional[dict]:
        """Submit a Fill-Or-Kill market order (TAKER). Pays the fee.
        Falls back here when a maker order doesn't fill before timeout.
        """
        if not self._authenticated:
            logger.info(f"[PAPER] {side} FOK {size:.1f}@{price:.3f}")
            return {"orderID": f"paper-fok-{int(time.time()*1000)}", "paper": True, "filled": True}

        if not self._check_rate_limit():
            logger.warning("rate limit")
            return None

        def _place():
            from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
            from py_clob_client.order_builder.constants import BUY, SELL
            tick_size = self._client.get_tick_size(token_id)
            options = PartialCreateOrderOptions(tick_size=tick_size)
            p = round(price / tick_size) * tick_size
            order_args = OrderArgs(
                token_id=token_id, price=p, size=size,
                side=BUY if side == "BUY" else SELL,
            )
            signed = self._client.create_order(order_args, options)
            # FOK = Fill-or-Kill: entire order fills at this price or nothing
            # post_only=False means we're a taker — we cross the book
            return self._client.post_order(signed, OrderType.FOK, post_only=False)

        try:
            result = await asyncio.to_thread(_place)
            self._order_timestamps.append(time.time())
            logger.info(f"FOK order: {side} {size}@{price:.3f} → {result.get('orderID','?')}")
            return result
        except Exception as e:
            logger.error(f"place_order_fok: {e}")
            return None

    async def get_order_status(self, order_id: str) -> Optional[dict]:
        """Fetch current status of a specific order. Returns None on error.
        Status values from py-clob-client: 'LIVE', 'MATCHED', 'CANCELLED', etc.
        """
        if not self._authenticated or not order_id or order_id.startswith("paper"):
            return None
        try:
            resp = await asyncio.to_thread(self._client.get_order, order_id)
            return resp if isinstance(resp, dict) else None
        except Exception as e:
            logger.debug(f"get_order_status: {e}")
            return None

    async def place_order_maker_first(
        self, token_id: str, price: float, size: float, side: str = "BUY",
        maker_offset: float = 0.005, timeout_sec: int = 90, poll_sec: int = 10,
    ) -> Optional[dict]:
        """Try to fill as maker first (0 fee + rebate), fall back to taker (FOK) if unfilled.

        Args:
            token_id: market token
            price: target fill price (taker price — the price we'd pay if we crossed book)
            size: shares desired
            side: BUY or SELL
            maker_offset: how far inside the spread to place our limit (default 0.5¢)
            timeout_sec: give up on maker fill after N seconds
            poll_sec: check fill status every N seconds

        Returns: dict with fill info, or None if both paths fail.
        Adds 'fill_mode' = 'maker' | 'taker' | 'none' so caller knows what happened.
        """
        # Maker price: better for us than the taker price (lower when buying, higher when selling)
        if side == "BUY":
            maker_price = max(0.01, price - maker_offset)
        else:
            maker_price = min(0.99, price + maker_offset)

        # 1. Try maker first
        maker_result = await self.place_order(token_id, maker_price, size, side)
        if not maker_result:
            logger.info(f"maker-first: maker submission failed, going direct taker")
            taker = await self.place_order_fok(token_id, price, size, side)
            if taker:
                taker["fill_mode"] = "taker"
            return taker

        order_id = maker_result.get("orderID")
        if not order_id or str(order_id).startswith("paper"):
            # Paper mode — pretend it filled at maker price instantly
            maker_result["fill_mode"] = "maker"
            maker_result["fill_price"] = maker_price
            return maker_result

        # 2. Poll for maker fill
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            await asyncio.sleep(poll_sec)
            status = await self.get_order_status(order_id)
            if not status:
                continue
            status_name = str(status.get("status", "")).upper()
            if status_name in ("MATCHED", "FILLED"):
                logger.info(f"maker-first: MAKER FILL {order_id} @{maker_price:.3f}")
                maker_result["fill_mode"] = "maker"
                maker_result["fill_price"] = maker_price
                return maker_result
            if status_name in ("CANCELLED", "REJECTED", "EXPIRED"):
                logger.info(f"maker-first: order {status_name}, falling to taker")
                break

        # 3. Timeout — cancel maker order, fall through to taker
        await self.cancel_order(order_id)
        logger.info(f"maker-first: MAKER TIMEOUT after {timeout_sec}s, falling to taker")
        taker = await self.place_order_fok(token_id, price, size, side)
        if taker:
            taker["fill_mode"] = "taker"
            taker["fill_price"] = price
        return taker

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
        # Diagnostic: check what parse_market_tokens returns for each filtered event
        structures = {"classic": 0, "soccer_3way": 0, "no_parse": 0}
        sample_parses = []
        for ev in filtered[:20]:
            parsed = parse_market_tokens(ev)
            if parsed is None:
                structures["no_parse"] += 1
                if len(sample_parses) < 5:
                    children = ev.get("markets", [])
                    child_q = [m.get("question", "")[:30] for m in children[:3]]
                    sample_parses.append({
                        "title": ev.get("title", "")[:60],
                        "status": "NO_PARSE",
                        "n_children": len(children),
                        "child_questions": child_q,
                    })
            else:
                structures[parsed.get("_structure", "classic")] += 1
                if len(sample_parses) < 5:
                    sample_parses.append({
                        "title": ev.get("title", "")[:60],
                        "status": parsed.get("_structure", "?"),
                        "outcomes": parsed.get("outcomes", [])[:2],
                    })
        self.poly_diag[sport] = {
            "tag": f"series={series_id}", "raw": len(raw), "filtered": len(filtered),
            "sample_titles": [ev.get("title", "") for ev in filtered[:10]],
            "structures": structures,
            "sample_parses": sample_parses,
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
                # Polymarket /sports returns: {id: <row>, sport: "nba", series: "10345"}
                # We want the `series` field, not `id` (which is a row number 1..183).
                sid = s.get("series") or s.get("seriesId")
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
    """Parse a Gamma event -> normalized dict with 2 outcomes/tokens/prices.

    Polymarket has TWO market structures we must handle:

    CLASSIC (MLB, NBA, NFL, tennis, etc):
      One child market with outcomes=["Team A","Team B"] and two tokens.

    SOCCER (EPL, Championship, La Liga 2, Portuguese, etc):
      Three separate Yes/No child markets per event:
        M1 "Will {Home} win?"  outcomes=[Yes, No]
        M2 "Will the match end in a draw?"  outcomes=[Yes, No]
        M3 "Will {Away} win?"  outcomes=[Yes, No]
      We synthesize a 2-outcome "moneyline" from the YES sides of M1 + M3.
      This lets downstream matchers treat both structures identically.

    Derivative markets (spread, totals, BTTS, correct score, handicap) are
    rejected in both structures.
    """
    try:
        child_markets = market.get("markets", [])
        if not child_markets:
            return None

        # Question patterns that indicate a derivative — reject these.
        DERIV_PATTERNS = (
            "spread:", "spread ", "(-", "(+",
            "total:", "totals:", "over/under", " over ", " under ",
            "handicap", "to score", "both teams", "clean sheet",
            "first goal", "correct score", "half-time", "halftime",
            "draw no bet", "double chance", "dnb", "btts",
            "corners", "bookings", "cards", "penalty",
            "1h moneyline", "2h moneyline",
            "first half moneyline", "second half moneyline",
            "1st quarter", "2nd quarter", "3rd quarter", "4th quarter",
            "1st period", "2nd period", "3rd period",
            "first 5 innings", "5 innings",
            "1st inning", "2nd inning",
            ": 1h ", ": 2h ", ": 1st half", ": 2nd half",
        )

        def is_derivative(q: str) -> bool:
            ql = (q or "").lower()
            return any(p in ql for p in DERIV_PATTERNS)

        def load_outcomes(m: dict) -> list:
            raw = m.get("outcomes", "[]")
            try:
                return json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                return []

        def load_tokens(m: dict) -> list:
            raw = m.get("clobTokenIds", "[]")
            try:
                return json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                return []

        def load_prices(m: dict) -> list:
            raw = m.get("outcomePrices", "[]")
            try:
                parsed = json.loads(raw) if isinstance(raw, str) else raw
                return [float(p) for p in parsed]
            except Exception:
                return []

        event_title = market.get("title", "")
        title_lower = event_title.lower()

        # REJECT events whose title indicates they ARE derivatives overall.
        # Polymarket lists many variants alongside the real moneyline:
        #   "Team A vs. Team B - More Markets" (totals O/U)
        #   "Team A vs. Team B - Exact Score" (goalscorer markets)
        #   "Team A vs. Team B: 1H Moneyline" (first-half only)
        #   "Team A vs. Team B: 1st Quarter Moneyline" (quarter only)
        # We want ONLY the full-game moneyline event.
        DERIV_TITLE_SUFFIXES = (
            "- more markets", "- exact score", "- goalscorer",
            "- total goals", "- total points", "- spread",
            ": 1h", ": 2h", ": 1st half", ": 2nd half",
            ": 1st quarter", ": 2nd quarter", ": 3rd quarter", ": 4th quarter",
            ": 1st period", ": 2nd period", ": 3rd period",
            ": 1st inning", ": first 5", ": 5 innings",
            ": moneyline", ": spread", ": total",
            ": first half", ": second half",
            "1h moneyline", "2h moneyline",
            "first half moneyline", "second half moneyline",
            "first 5 innings",
        )
        if any(suf in title_lower for suf in DERIV_TITLE_SUFFIXES):
            return None

        # ─── Try CLASSIC structure first ──────────────────────────────────
        for m in child_markets:
            if m.get("closed"):
                continue
            q = m.get("question") or ""
            if is_derivative(q):
                continue
            outs = load_outcomes(m)
            if len(outs) != 2:
                continue
            # Classic moneyline: outcomes are team names.
            # Reject yes/no (soccer 3-way child), over/under (totals),
            # under/over, draw (should be in a separate child).
            low = [str(o).lower() for o in outs]
            NON_TEAM_OUTCOMES = {"yes", "no", "over", "under", "draw", "tie"}
            if any(o in NON_TEAM_OUTCOMES for o in low):
                continue
            toks = load_tokens(m)
            prices = load_prices(m)
            if len(toks) < 2 or len(prices) < 2:
                continue
            return {
                "condition_id": m.get("conditionId", ""),
                "question": q or event_title,
                "outcomes": list(outs[:2]),
                "token_ids": list(toks[:2]),
                "prices": list(prices[:2]),
                "end_date": m.get("endDate", ""),
                "volume": float(market.get("volume", 0) or 0),
                "liquidity": float(market.get("liquidity", 0) or 0),
                "last_trade_time": m.get("lastTradeTime") or m.get("last_trade_time"),
                "_structure": "classic",
            }

        # ─── Try SOCCER (three-way Yes/No) structure ──────────────────────
        # Find "Will X win?" markets — skip "draw" and skip derivatives.
        team_win_markets = []
        for m in child_markets:
            if m.get("closed"):
                continue
            q = (m.get("question") or "").strip()
            ql = q.lower()
            if is_derivative(q):
                continue
            # Want a "Will <team> win" phrasing. Reject the draw market explicitly.
            if "draw" in ql:
                continue
            if "will " not in ql or " win" not in ql:
                continue
            outs = load_outcomes(m)
            if len(outs) != 2:
                continue
            # Outcomes must be Yes/No
            low = [str(o).lower() for o in outs]
            if low[0] != "yes" or low[1] != "no":
                continue
            toks = load_tokens(m)
            prices = load_prices(m)
            if len(toks) < 2 or len(prices) < 2:
                continue
            # Extract team name from question: "Will <Team> win on ...?" or "Will <Team> win?"
            # Strip the "Will " prefix and " win..." suffix.
            team = q
            if team.lower().startswith("will "):
                team = team[5:]
            # Remove trailing "win..." portion
            team_lower = team.lower()
            win_idx = team_lower.find(" win")
            if win_idx > 0:
                team = team[:win_idx]
            team = team.strip().rstrip("?").strip()
            if not team:
                continue
            team_win_markets.append({
                "team": team,
                "yes_token": toks[0],
                "yes_price": prices[0],
                "condition_id": m.get("conditionId", ""),
                "last_trade_time": m.get("lastTradeTime") or m.get("last_trade_time"),
                "question": q,
            })

        if len(team_win_markets) >= 2:
            # Take the first two (should be exactly Home and Away — soccer events
            # have exactly two "team win" markets plus one draw market).
            a, b = team_win_markets[0], team_win_markets[1]
            return {
                # Use first team's condition_id as the "primary" — has_position_for_game
                # uses team names anyway, so duplicate-bet prevention still works.
                "condition_id": a["condition_id"],
                "question": event_title or f"{a['team']} vs. {b['team']}",
                "outcomes": [a["team"], b["team"]],
                "token_ids": [a["yes_token"], b["yes_token"]],
                "prices": [a["yes_price"], b["yes_price"]],
                "end_date": "",
                "volume": float(market.get("volume", 0) or 0),
                "liquidity": float(market.get("liquidity", 0) or 0),
                "last_trade_time": a["last_trade_time"],
                "_structure": "soccer_3way",
                # Expose per-team condition_ids so downstream code can track
                # the actual market being traded, not just the "primary".
                "_condition_ids": [a["condition_id"], b["condition_id"]],
            }

        return None
    except Exception as e:
        logger.debug(f"parse_market_tokens: {e}")
        return None
