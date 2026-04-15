"""
positions.py — Position Tracking, Resolution, and Persistence
Bankroll on restart = STARTING_BANKROLL - open_cost + total_pnl
"""

import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict, fields as dc_fields
from typing import Optional

from config import STARTING_BANKROLL, REDIS_URL

logger = logging.getLogger("positions")

STATE_FILE = "state.json"


@dataclass
class Position:
    """An open position awaiting resolution."""
    id: str
    engine: str
    sport: str
    market_question: str
    condition_id: str
    team: str
    side: str
    token_id: str
    entry_price: float
    size: float
    cost: float
    confidence: float
    order_id: str
    status: str = "open"
    fill_price: Optional[float] = None
    opened_at: float = field(default_factory=time.time)
    filled_at: Optional[float] = None
    espn_id: str = ""
    score_line: str = ""
    pinnacle_prob: float = 0.0
    edge: float = 0.0


@dataclass
class Trade:
    """A resolved (closed) trade with final P&L."""
    id: str
    engine: str
    sport: str
    market_question: str
    team: str
    side: str
    entry_price: float
    size: float
    cost: float
    confidence: float
    result: str
    payout: float
    pnl: float
    pnl_pct: float
    opened_at: float
    closed_at: float
    score_line: str = ""
    pinnacle_prob: float = 0.0
    edge: float = 0.0


def _safe_construct(cls, data: dict):
    """
    Construct a dataclass from a dict, ignoring unknown fields
    and supplying defaults for missing fields. Survives schema evolution.
    """
    valid_fields = {f.name for f in dc_fields(cls)}
    filtered = {k: v for k, v in data.items() if k in valid_fields}
    return cls(**filtered)


class PositionManager:
    """Manages open positions, trade history, and equity calculations."""

    def __init__(self):
        self.positions: dict[str, Position] = {}
        self.trades: list[Trade] = []
        self._redis = None
        self._redis_available = False

    async def initialize(self):
        await self._init_redis()
        await self._restore_state()
        logger.info(
            f"PositionManager: {len(self.positions)} open, "
            f"{len(self.trades)} resolved, equity=${self.equity:.2f}"
        )

    async def _init_redis(self):
        if not REDIS_URL:
            logger.info("No REDIS_URL — using file persistence")
            return
        try:
            import redis.asyncio as aioredis
            self._redis = aioredis.from_url(REDIS_URL, decode_responses=True)
            await self._redis.ping()
            self._redis_available = True
            logger.info("Redis connected")
        except Exception as e:
            logger.warning(f"Redis unavailable ({e}) — falling back to file")
            self._redis = None
            self._redis_available = False

    def _serialize_state(self) -> str:
        return json.dumps({
            "positions": {pid: asdict(p) for pid, p in self.positions.items()},
            "trades": [asdict(t) for t in self.trades],
        }, default=str)

    async def _save_state(self):
        data = self._serialize_state()
        try:
            if self._redis_available:
                await self._redis.set("signal:state", data)
            else:
                with open(STATE_FILE, "w") as f:
                    f.write(data)
        except Exception as e:
            logger.error(f"Failed to save state: {e}")

    async def _restore_state(self):
        raw = None
        try:
            if self._redis_available:
                raw = await self._redis.get("signal:state")
            elif os.path.exists(STATE_FILE):
                with open(STATE_FILE, "r") as f:
                    raw = f.read()
        except Exception as e:
            logger.error(f"Failed to read state: {e}")

        if not raw:
            logger.info("No saved state — starting fresh")
            return

        try:
            data = json.loads(raw)
            for pid, pdata in data.get("positions", {}).items():
                self.positions[pid] = _safe_construct(Position, pdata)
            for tdata in data.get("trades", []):
                self.trades.append(_safe_construct(Trade, tdata))
            logger.info(f"Restored {len(self.positions)} positions, {len(self.trades)} trades")
        except Exception as e:
            logger.error(f"Failed to parse saved state: {e}")

    # ─── EQUITY ──────────────────────────────────────────────────────────

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl for t in self.trades)

    @property
    def open_cost(self) -> float:
        return sum(p.cost for p in self.positions.values() if p.status in ("open", "filled"))

    @property
    def equity(self) -> float:
        return STARTING_BANKROLL - self.open_cost + self.total_pnl

    @property
    def total_equity(self) -> float:
        return STARTING_BANKROLL + self.total_pnl

    # ─── POSITION MANAGEMENT ────────────────────────────────────────────

    async def open_position(self, position: Position):
        self.positions[position.id] = position
        await self._save_state()
        logger.info(
            f"Opened {position.id}: {position.engine} | "
            f"{position.team} {position.side}@{position.entry_price} x{position.size}"
        )

    async def mark_filled(self, position_id: str, fill_price: Optional[float] = None):
        pos = self.positions.get(position_id)
        if not pos:
            return
        pos.status = "filled"
        pos.filled_at = time.time()
        if fill_price is not None:
            pos.fill_price = fill_price
            pos.cost = fill_price * pos.size
        await self._save_state()

    async def cancel_position(self, position_id: str):
        if position_id not in self.positions:
            return
        del self.positions[position_id]
        await self._save_state()
        logger.info(f"Cancelled position {position_id}")

    async def resolve_position(self, position_id: str, winner: str):
        pos = self.positions.get(position_id)
        if not pos:
            return

        if winner == pos.side:
            result = "WIN"
            payout = pos.size * 1.0
        elif winner == "UNKNOWN":
            result = "PUSH"
            payout = pos.cost
        else:
            result = "LOSS"
            payout = 0.0

        pnl = payout - pos.cost
        pnl_pct = (pnl / pos.cost) if pos.cost > 0 else 0.0

        trade = Trade(
            id=pos.id, engine=pos.engine, sport=pos.sport,
            market_question=pos.market_question, team=pos.team, side=pos.side,
            entry_price=pos.fill_price or pos.entry_price,
            size=pos.size, cost=pos.cost, confidence=pos.confidence,
            result=result, payout=payout, pnl=pnl, pnl_pct=pnl_pct,
            opened_at=pos.opened_at, closed_at=time.time(),
            score_line=pos.score_line, pinnacle_prob=pos.pinnacle_prob, edge=pos.edge,
        )

        self.trades.append(trade)
        del self.positions[position_id]
        await self._save_state()

        emoji = "✅" if result == "WIN" else "❌" if result == "LOSS" else "↩️"
        logger.info(f"{emoji} {pos.id}: {result} | PnL=${pnl:+.2f} ({pnl_pct:+.1%})")

    # ─── QUERIES ─────────────────────────────────────────────────────────

    def has_position_for(self, condition_id: str) -> bool:
        return any(
            p.condition_id == condition_id
            for p in self.positions.values()
            if p.status in ("open", "filled")
        )

    def get_stale_orders(self, max_age: int) -> list[Position]:
        now = time.time()
        return [
            p for p in self.positions.values()
            if p.status == "open" and (now - p.opened_at) > max_age
        ]

    def get_open_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if p.status in ("open", "filled")]

    def stats(self, engine: Optional[str] = None) -> dict:
        trades = self.trades if not engine else [t for t in self.trades if t.engine == engine]
        positions = self.get_open_positions()
        if engine:
            positions = [p for p in positions if p.engine == engine]

        wins = [t for t in trades if t.result == "WIN"]
        losses = [t for t in trades if t.result == "LOSS"]
        total = len(wins) + len(losses)

        return {
            "total_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / total if total > 0 else 0.0,
            "total_pnl": sum(t.pnl for t in trades),
            "avg_pnl": sum(t.pnl for t in trades) / len(trades) if trades else 0.0,
            "best_trade": max((t.pnl for t in trades), default=0.0),
            "worst_trade": min((t.pnl for t in trades), default=0.0),
            "open_positions": len(positions),
            "open_cost": sum(p.cost for p in positions),
        }
