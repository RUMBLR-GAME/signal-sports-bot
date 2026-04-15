"""
positions.py — Position Tracking with Exit Management
Now tracks game_start_time for Edge Finder pre-game exits.
"""

import json, logging, os, time
from dataclasses import dataclass, field, asdict, fields as dc_fields
from typing import Optional
from config import STARTING_BANKROLL, REDIS_URL

logger = logging.getLogger("positions")
STATE_FILE = "state.json"


@dataclass
class Position:
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
    true_prob: float = 0.0
    edge_at_entry: float = 0.0
    game_start_time: str = ""    # ISO 8601 — when the game starts (Edge Finder)
    score_line: str = ""
    espn_id: str = ""
    exit_reason: str = ""


@dataclass
class Trade:
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
    true_prob: float = 0.0
    edge_at_entry: float = 0.0
    exit_reason: str = ""


def _safe_construct(cls, data: dict):
    valid = {f.name for f in dc_fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in valid})


class PositionManager:
    def __init__(self):
        self.positions: dict[str, Position] = {}
        self.trades: list[Trade] = []
        self._redis = None
        self._redis_ok = False

    async def initialize(self):
        if REDIS_URL:
            try:
                import redis.asyncio as ar
                self._redis = ar.from_url(REDIS_URL, decode_responses=True)
                await self._redis.ping()
                self._redis_ok = True
            except Exception:
                self._redis = None
        await self._restore()
        logger.info(f"Positions: {len(self.positions)} open, {len(self.trades)} resolved, equity=${self.equity:.2f}")

    async def _save(self):
        d = json.dumps({"positions": {p: asdict(v) for p, v in self.positions.items()}, "trades": [asdict(t) for t in self.trades]}, default=str)
        try:
            if self._redis_ok:
                await self._redis.set("signal:state", d)
            else:
                with open(STATE_FILE, "w") as f:
                    f.write(d)
        except Exception as e:
            logger.error(f"Save: {e}")

    async def _restore(self):
        raw = None
        try:
            if self._redis_ok:
                raw = await self._redis.get("signal:state")
            elif os.path.exists(STATE_FILE):
                with open(STATE_FILE) as f:
                    raw = f.read()
        except Exception:
            pass
        if not raw:
            return
        try:
            d = json.loads(raw)
            for pid, pd in d.get("positions", {}).items():
                self.positions[pid] = _safe_construct(Position, pd)
            for td in d.get("trades", []):
                self.trades.append(_safe_construct(Trade, td))
        except Exception as e:
            logger.error(f"Restore: {e}")

    @property
    def total_pnl(self):
        return sum(t.pnl for t in self.trades)

    @property
    def open_cost(self):
        return sum(p.cost for p in self.positions.values() if p.status in ("open", "filled"))

    @property
    def equity(self):
        return STARTING_BANKROLL - self.open_cost + self.total_pnl

    @property
    def total_equity(self):
        return STARTING_BANKROLL + self.total_pnl

    async def open_position(self, pos: Position):
        self.positions[pos.id] = pos
        await self._save()
        logger.info(f"Opened {pos.id}: {pos.engine} | {pos.team} {pos.side}@{pos.entry_price} x{pos.size:.0f}")

    async def mark_filled(self, pid, fill_price=None):
        p = self.positions.get(pid)
        if not p:
            return
        p.status = "filled"
        p.filled_at = time.time()
        if fill_price is not None:
            p.fill_price = fill_price
            p.cost = fill_price * p.size
        await self._save()

    async def cancel_position(self, pid):
        if pid in self.positions:
            del self.positions[pid]
            await self._save()

    async def resolve_position(self, pid, winner):
        p = self.positions.get(pid)
        if not p:
            return
        if winner == p.side or (winner == "YES" and p.side == p.team):
            result, payout = "WIN", p.size * 1.0
        elif winner == "UNKNOWN":
            result, payout = "PUSH", p.cost
        else:
            result, payout = "LOSS", 0.0
        await self._close(p, result, payout, "resolution")

    async def exit_position(self, pid, exit_price, reason):
        p = self.positions.get(pid)
        if not p:
            return
        payout = exit_price * p.size
        result = "EXIT_PROFIT" if payout >= p.cost else "EXIT_LOSS"
        await self._close(p, result, payout, reason)

    async def _close(self, p, result, payout, reason):
        pnl = payout - p.cost
        pnl_pct = pnl / p.cost if p.cost > 0 else 0
        self.trades.append(Trade(
            id=p.id, engine=p.engine, sport=p.sport, market_question=p.market_question,
            team=p.team, side=p.side, entry_price=p.fill_price or p.entry_price,
            size=p.size, cost=p.cost, confidence=p.confidence,
            result=result, payout=payout, pnl=pnl, pnl_pct=pnl_pct,
            opened_at=p.opened_at, closed_at=time.time(),
            score_line=p.score_line, true_prob=p.true_prob,
            edge_at_entry=p.edge_at_entry, exit_reason=reason,
        ))
        del self.positions[p.id]
        await self._save()
        sym = {"WIN":"✅","LOSS":"❌","PUSH":"↩️","EXIT_PROFIT":"💰","EXIT_LOSS":"🛑"}.get(result, "?")
        logger.info(f"{sym} {p.id}: {result} ${pnl:+.2f} ({pnl_pct:+.1%}) [{reason}]")

    def has_position_for(self, cid):
        return any(p.condition_id == cid for p in self.positions.values() if p.status in ("open", "filled"))

    def get_stale_orders(self, max_age):
        now = time.time()
        return [p for p in self.positions.values() if p.status == "open" and (now - p.opened_at) > max_age]

    def get_open_positions(self):
        return [p for p in self.positions.values() if p.status in ("open", "filled")]

    def get_filled_by_engine(self, engine):
        return [p for p in self.positions.values() if p.status == "filled" and p.engine == engine]

    def stats(self, engine=None):
        trades = self.trades if not engine else [t for t in self.trades if t.engine == engine]
        positions = self.get_open_positions()
        if engine:
            positions = [p for p in positions if p.engine == engine]
        wins = [t for t in trades if t.result in ("WIN", "EXIT_PROFIT")]
        losses = [t for t in trades if t.result in ("LOSS", "EXIT_LOSS")]
        total = len(wins) + len(losses)
        return {
            "total_trades": len(trades), "wins": len(wins), "losses": len(losses),
            "win_rate": len(wins) / total if total > 0 else 0.0,
            "total_pnl": sum(t.pnl for t in trades),
            "avg_pnl": sum(t.pnl for t in trades) / len(trades) if trades else 0.0,
            "best_trade": max((t.pnl for t in trades), default=0.0),
            "worst_trade": min((t.pnl for t in trades), default=0.0),
            "open_positions": len(positions), "open_cost": sum(p.cost for p in positions),
        }
