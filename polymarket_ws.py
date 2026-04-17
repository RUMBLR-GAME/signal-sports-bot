"""
polymarket_ws.py — Polymarket Sports & Market WebSocket client (v18 NEW)

This is the single biggest improvement from v17 → v18.

Two WebSockets:
  • Sports WS  (wss://ws-live-data.polymarket.com) — real-time game state
    (scores, period, clock). ~100ms latency. No auth required.
    REPLACES ESPN as the primary live signal source.
  • Market WS (wss://ws-subscriptions-clob.polymarket.com/ws/market) — live
    orderbook + last-trade-price per token. Subscribe to tokens we care about
    and get push updates instead of polling REST.

Why this matters:
  v17 polled ESPN every 30s + Gamma every scan. Total signal-to-trade latency
  ~30-90s. By the time a blowout was detected and executed, the price had
  already moved.

  v18 gets scores in ~100ms and live prices pushed. Signal-to-trade ~200-500ms.
  Not HFT (that requires colocation), but massively better for Brisbane.

Design:
  • Both connections auto-reconnect with exponential backoff.
  • Sports feed emits normalized game events via callback.
  • Market feed maintains local orderbook snapshots per token.
  • All state thread-safe via asyncio.
  • If WS fails, bot falls back to REST (ESPN + Gamma) automatically.
"""
import asyncio
import json
import logging
import random
import time
from typing import Callable, Dict, Optional, List, Set

try:
    import aiohttp
except ImportError:
    aiohttp = None

logger = logging.getLogger("poly_ws")

SPORTS_WS_URL = "wss://ws-live-data.polymarket.com"
MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class _BackoffReconnect:
    """Exponential backoff with jitter."""
    def __init__(self, base: float = 1.0, cap: float = 30.0):
        self.base = base
        self.cap = cap
        self.attempt = 0

    def reset(self):
        self.attempt = 0

    def next_delay(self) -> float:
        self.attempt += 1
        d = min(self.cap, self.base * (2 ** min(self.attempt, 6)))
        return d * (0.5 + random.random() * 0.5)  # jitter


class SportsWS:
    """
    Connects to Polymarket's sports WebSocket and streams game updates.
    Call on_update(fn) to receive normalized game dicts.

    The server sends ping every 5s — we respond with pong.
    """
    def __init__(self):
        self._callbacks: List[Callable] = []
        self._task: Optional[asyncio.Task] = None
        self._ws = None
        self._last_msg_at: float = 0.0
        self._games: Dict[str, dict] = {}  # espn_id → game dict
        self._backoff = _BackoffReconnect()
        self._running = False

    def on_update(self, fn: Callable):
        """Register callback(game_dict). Called whenever game state changes."""
        self._callbacks.append(fn)

    def latest_games(self) -> List[dict]:
        """Snapshot of all currently-tracked games."""
        return list(self._games.values())

    def is_connected(self) -> bool:
        return self._ws is not None and not getattr(self._ws, "closed", True)

    def seconds_since_last_message(self) -> float:
        return time.time() - self._last_msg_at if self._last_msg_at else 1e9

    async def start(self, session: "aiohttp.ClientSession"):
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._run(session))

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self, session):
        while self._running:
            try:
                async with session.ws_connect(
                    SPORTS_WS_URL, heartbeat=30, timeout=20
                ) as ws:
                    self._ws = ws
                    self._backoff.reset()
                    logger.info("SportsWS connected")
                    async for msg in ws:
                        self._last_msg_at = time.time()
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = msg.data
                            if data == "ping":
                                await ws.send_str("pong")
                                continue
                            await self._handle_text(data)
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            logger.warning(f"SportsWS error: {ws.exception()}")
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"SportsWS crashed: {e}")
            finally:
                self._ws = None

            if not self._running:
                break
            delay = self._backoff.next_delay()
            logger.info(f"SportsWS reconnecting in {delay:.1f}s")
            await asyncio.sleep(delay)

    async def _handle_text(self, data: str):
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            return
        # Polymarket sports WS format varies; defensively extract common fields.
        game = self._normalize(obj)
        if not game:
            return
        gid = game.get("game_id") or game.get("espn_id") or str(hash(
            (game.get("home_team",""), game.get("away_team",""), game.get("start_time",""))
        ))
        game["game_id"] = gid
        prev = self._games.get(gid, {})
        # Only fire callbacks on meaningful change
        meaningful = any(
            prev.get(k) != game.get(k)
            for k in ("home_score", "away_score", "period", "clock", "state", "status")
        )
        self._games[gid] = {**prev, **game, "last_update": time.time()}
        if meaningful or not prev:
            for cb in self._callbacks:
                try:
                    cb(self._games[gid])
                except Exception as e:
                    logger.warning(f"callback error: {e}")

    @staticmethod
    def _normalize(obj: dict) -> Optional[dict]:
        """
        Best-effort normalization. Polymarket's sports WS message schema
        isn't fully documented and varies. We look for common fields.
        """
        if not isinstance(obj, dict):
            return None
        # Various possible shapes
        g = obj.get("game") if isinstance(obj.get("game"), dict) else obj
        if not isinstance(g, dict):
            return None
        home = g.get("home_team") or g.get("homeTeam") or g.get("home", {})
        away = g.get("away_team") or g.get("awayTeam") or g.get("away", {})
        if isinstance(home, dict):
            home_name = home.get("name") or home.get("displayName", "")
            home_score = int(home.get("score", 0) or 0)
            home_ab = home.get("abbreviation", "")
        else:
            home_name = str(home or "")
            home_score = int(g.get("home_score", 0) or 0)
            home_ab = g.get("home_abbrev", "")
        if isinstance(away, dict):
            away_name = away.get("name") or away.get("displayName", "")
            away_score = int(away.get("score", 0) or 0)
            away_ab = away.get("abbreviation", "")
        else:
            away_name = str(away or "")
            away_score = int(g.get("away_score", 0) or 0)
            away_ab = g.get("away_abbrev", "")
        if not home_name or not away_name:
            return None
        return {
            "home_team": home_name,
            "away_team": away_name,
            "home_abbrev": home_ab,
            "away_abbrev": away_ab,
            "home_score": home_score,
            "away_score": away_score,
            "period": g.get("period", 0),
            "clock": g.get("clock") or g.get("displayClock", ""),
            "state": g.get("state") or g.get("status", ""),
            "sport": g.get("sport") or g.get("league") or "",
            "espn_id": g.get("espn_id") or g.get("id", ""),
            "start_time": g.get("start_time") or g.get("startTime") or g.get("date", ""),
        }


class MarketWS:
    """
    Subscribe to Polymarket market orderbooks / price changes for specific tokens.
    Maintains local orderbook per token with best bid/ask and last trade price.

    Use:
      await mw.subscribe([tok1, tok2, ...])
      bid, ask = mw.best(token_id)       # best bid & ask
      mid = mw.midpoint(token_id)
      last = mw.last_trade(token_id)
    """
    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._ws = None
        self._running = False
        self._subscribed: Set[str] = set()
        self._pending_subs: Set[str] = set()
        self._books: Dict[str, dict] = {}   # token → {best_bid, best_ask, bids[], asks[], last_trade, last_update}
        self._backoff = _BackoffReconnect()

    def is_connected(self) -> bool:
        return self._ws is not None and not getattr(self._ws, "closed", True)

    async def start(self, session: "aiohttp.ClientSession"):
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._run(session))

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def subscribe(self, token_ids: List[str]):
        """Subscribe to a list of token IDs. Safe to call repeatedly."""
        new = [t for t in token_ids if t and t not in self._subscribed]
        if not new:
            return
        self._pending_subs.update(new)
        if self._ws is not None and not self._ws.closed:
            await self._send_subscribe(new)

    async def _send_subscribe(self, token_ids: List[str]):
        if not self._ws or self._ws.closed:
            return
        try:
            await self._ws.send_str(json.dumps({
                "type": "market",
                "assets_ids": token_ids,
            }))
            self._subscribed.update(token_ids)
            for t in token_ids:
                self._pending_subs.discard(t)
        except Exception as e:
            logger.warning(f"MarketWS subscribe fail: {e}")

    def best(self, token_id: str) -> tuple:
        b = self._books.get(token_id)
        if not b:
            return (None, None)
        return (b.get("best_bid"), b.get("best_ask"))

    def midpoint(self, token_id: str) -> Optional[float]:
        bid, ask = self.best(token_id)
        if bid is None or ask is None:
            return None
        # Reject empty/degenerate books: if bid=0 and ask=1, the midpoint
        # looks like 0.5 but is meaningless — there's no actual market here.
        # Also reject any market with a spread > 0.5 as untrustworthy.
        if ask - bid > 0.5:
            return None
        return (bid + ask) / 2.0

    def last_trade(self, token_id: str) -> Optional[float]:
        b = self._books.get(token_id)
        return b.get("last_trade") if b else None

    def book_age(self, token_id: str) -> float:
        b = self._books.get(token_id)
        if not b or "last_update" not in b:
            return 1e9
        return time.time() - b["last_update"]

    async def _run(self, session):
        while self._running:
            try:
                async with session.ws_connect(
                    MARKET_WS_URL, heartbeat=30, timeout=20
                ) as ws:
                    self._ws = ws
                    self._backoff.reset()
                    logger.info("MarketWS connected")
                    # Re-subscribe everything
                    to_sub = list(self._subscribed | self._pending_subs)
                    if to_sub:
                        await ws.send_str(json.dumps({
                            "type": "market", "assets_ids": to_sub,
                        }))
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = msg.data
                            if data == "ping":
                                await ws.send_str("pong")
                                continue
                            self._handle_market(data)
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            logger.warning(f"MarketWS error: {ws.exception()}")
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"MarketWS crashed: {e}")
            finally:
                self._ws = None

            if not self._running:
                break
            delay = self._backoff.next_delay()
            logger.info(f"MarketWS reconnecting in {delay:.1f}s")
            await asyncio.sleep(delay)

    def _handle_market(self, data: str):
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            return
        events = obj if isinstance(obj, list) else [obj]
        now = time.time()
        for ev in events:
            if not isinstance(ev, dict):
                continue
            tok = ev.get("asset_id") or ev.get("assetId") or ev.get("token_id")
            if not tok:
                continue
            b = self._books.setdefault(tok, {
                "best_bid": None, "best_ask": None,
                "bids": [], "asks": [],
                "last_trade": None, "last_update": 0,
            })
            etype = ev.get("event_type") or ev.get("type")

            if etype == "book":
                bids = ev.get("bids") or []
                asks = ev.get("asks") or []
                # bids: descending, asks: ascending
                def _price(x):
                    try:
                        return float(x.get("price"))
                    except (TypeError, ValueError):
                        return None
                b["bids"] = [(float(x.get("price",0)), float(x.get("size",0))) for x in bids[:10]]
                b["asks"] = [(float(x.get("price",0)), float(x.get("size",0))) for x in asks[:10]]
                b["best_bid"] = b["bids"][0][0] if b["bids"] else None
                b["best_ask"] = b["asks"][0][0] if b["asks"] else None
            elif etype == "price_change":
                changes = ev.get("changes") or ([ev] if "price" in ev else [])
                for c in changes:
                    try:
                        price = float(c.get("price"))
                        size = float(c.get("size", 0))
                        side = c.get("side", "").upper()
                    except (TypeError, ValueError):
                        continue
                    if side == "BUY":
                        # Update bids
                        b["bids"] = _apply_level(b["bids"], price, size, descending=True)
                        b["best_bid"] = b["bids"][0][0] if b["bids"] else None
                    elif side == "SELL":
                        b["asks"] = _apply_level(b["asks"], price, size, descending=False)
                        b["best_ask"] = b["asks"][0][0] if b["asks"] else None
            elif etype == "last_trade_price":
                try:
                    b["last_trade"] = float(ev.get("price"))
                except (TypeError, ValueError):
                    pass
            b["last_update"] = now


def _apply_level(levels: list, price: float, size: float, descending: bool) -> list:
    """Update a single level in a sorted level list."""
    out = [(p, s) for (p, s) in levels if p != price]
    if size > 0:
        out.append((price, size))
    out.sort(key=lambda x: x[0], reverse=descending)
    return out[:10]
