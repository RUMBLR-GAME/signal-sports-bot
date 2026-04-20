"""
positions.py — Position & Trade State Management (v18)

Fixes from v17:
  • equity() was STARTING_BANKROLL - open_cost + total_pnl → always depressed.
    Now equity() = cash + mark-to-market of open positions. Correct.
  • resolve_position() checked winner == p.side but p.side was stored
    inconsistently (YES/NO vs outcome name). Now uses a dedicated
    `bet_outcome` field that's always the outcome name we bet on,
    plus `bet_is_yes_side` for resolution.
  • Added partial_close() for Harvest partial exits.
  • Added mark-to-market support via current_price on each position.
  • Added circuit breaker state (consec losses, daily P&L anchor).
  • Adds peak_equity tracking for drawdown governor.
  • Adds per-sport / per-time-window exposure helpers (correlation caps).
"""
import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict, fields as dc_fields
from typing import Optional, Tuple

from config import (
    STARTING_BANKROLL, REDIS_URL, FORCE_RESET,
    EQUITY_CURVE_MAX, TRADE_HISTORY_MAX,
    CIRCUIT_DAILY_LOSS_LIMIT, CIRCUIT_CONSECUTIVE_LOSSES, CIRCUIT_COOLDOWN_MIN,
    CORRELATION_WINDOW_HOURS,
)

logger = logging.getLogger("positions")
STATE_FILE = os.getenv("STATE_FILE", "state.json")


@dataclass
class Position:
    id: str
    engine: str              # harvest | edge
    sport: str
    market_question: str
    condition_id: str
    team: str                # human-readable team we bet on
    bet_outcome: str         # the exact outcome string (e.g. "Lakers")
    bet_is_yes_side: bool    # True if we bought the YES token of a binary YES/NO market
    outcome_idx: int         # which outcome index in the market we bet on
    token_id: str
    entry_price: float
    size: float              # shares
    cost: float              # $ deployed
    confidence: float
    order_id: str
    status: str = "open"     # open | filled | (resolved entries are moved to trades)
    fill_price: Optional[float] = None
    opened_at: float = field(default_factory=time.time)
    filled_at: Optional[float] = None
    true_prob: float = 0.0
    edge_at_entry: float = 0.0
    game_start_time: str = ""
    score_line: str = ""
    espn_id: str = ""
    exit_reason: str = ""
    # Mark-to-market (written by monitor loop)
    current_price: Optional[float] = None
    last_mark_at: Optional[float] = None
    partial_exits: int = 0
    # Bookmaker context (edge trades only — shows sharp-book pricing vs Polymarket)
    provider: str = ""           # e.g. "Bet365", "DraftKings"
    moneyline: int = 0           # American moneyline odds at entry (e.g. +150, -120)
    # CLV (closing line value) — snapshot of book's line at kickoff, set by pre-game exit
    # If positive: bookmaker line moved TOWARD our side → we had real alpha
    clv_prob: Optional[float] = None
    clv_snapshot_at: Optional[float] = None


@dataclass
class Trade:
    id: str
    engine: str
    sport: str
    market_question: str
    team: str
    bet_outcome: str
    entry_price: float
    exit_price: float
    size: float             # shares closed in this trade
    cost: float             # $ cost that got closed
    confidence: float
    result: str             # WIN | LOSS | PUSH | EXIT_PROFIT | EXIT_LOSS | PARTIAL
    payout: float
    pnl: float
    pnl_pct: float
    opened_at: float
    closed_at: float
    score_line: str = ""
    true_prob: float = 0.0
    edge_at_entry: float = 0.0
    exit_reason: str = ""
    # CLV for alpha tracking — copied from Position at exit time
    provider: str = ""
    entry_book_prob: float = 0.0     # book-implied prob at entry (for CLV calc)
    clv_prob: Optional[float] = None  # book-implied prob at kickoff
    clv_edge: Optional[float] = None  # entry_book_prob - clv_prob; positive = beat close


def _safe_construct(cls, data: dict):
    valid = {f.name for f in dc_fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in valid})


def _ml_to_prob_safe(ml) -> float:
    """American moneyline → implied probability (no de-vig). 0 for invalid input."""
    if not ml:
        return 0.0
    try:
        ml = int(ml)
    except (TypeError, ValueError):
        return 0.0
    if ml == 0:
        return 0.0
    if ml > 0:
        return round(100.0 / (ml + 100.0), 4)
    return round(abs(ml) / (abs(ml) + 100.0), 4)


def _calc_clv_edge(entry_ml, clv_prob) -> Optional[float]:
    """
    CLV edge = entry_book_prob - closing_book_prob.
    Positive: we bet at a better price than the close → real alpha.
    Negative: book moved away from us → we bet at the wrong side.
    """
    if clv_prob is None:
        return None
    entry_prob = _ml_to_prob_safe(entry_ml)
    if entry_prob == 0:
        return None
    return round(entry_prob - clv_prob, 4)


class PositionManager:
    def __init__(self):
        self.positions: dict[str, Position] = {}
        self.trades: list[Trade] = []
        self.cash: float = STARTING_BANKROLL
        self.peak_equity: float = STARTING_BANKROLL
        self.equity_curve: list[list] = []   # [[ts, equity], ...]
        self.circuit: dict = {
            "tripped": False,
            "until_ts": 0.0,
            "consec_losses": 0,
            "daily_anchor_equity": STARTING_BANKROLL,
            "daily_date": "",
        }
        self._redis = None
        self._redis_ok = False

    # ── Init / persistence ───────────────────────────────────────────────
    async def initialize(self):
        if REDIS_URL:
            try:
                import redis.asyncio as ar
                self._redis = ar.from_url(REDIS_URL, decode_responses=True)
                await self._redis.ping()
                self._redis_ok = True
                logger.info("Redis connected")
            except Exception as e:
                logger.warning(f"Redis connect failed: {e}")
                self._redis = None

        if FORCE_RESET:
            logger.warning("FORCE_RESET=true — clearing state")
            await self._reset()
        else:
            await self._restore()

        # Initial curve point
        self.record_equity_point()
        logger.info(
            f"Positions: {len(self.positions)} open, {len(self.trades)} resolved, "
            f"cash=${self.cash:.2f} equity=${self.equity:.2f}"
        )

    async def _reset(self):
        self.positions = {}
        self.trades = []
        self.cash = STARTING_BANKROLL
        self.peak_equity = STARTING_BANKROLL
        self.equity_curve = []
        self.circuit = {
            "tripped": False, "until_ts": 0.0, "consec_losses": 0,
            "daily_anchor_equity": STARTING_BANKROLL, "daily_date": "",
        }
        if self._redis_ok:
            try:
                await self._redis.delete("signal:state")
            except Exception:
                pass
        try:
            if os.path.exists(STATE_FILE):
                os.remove(STATE_FILE)
        except Exception:
            pass
        await self._save()

    async def _save(self):
        data = json.dumps({
            "positions": {p: asdict(v) for p, v in self.positions.items()},
            "trades": [asdict(t) for t in self.trades[-TRADE_HISTORY_MAX:]],
            "cash": self.cash,
            "peak_equity": self.peak_equity,
            "equity_curve": self.equity_curve[-EQUITY_CURVE_MAX:],
            "circuit": self.circuit,
        }, default=str)
        try:
            if self._redis_ok:
                await self._redis.set("signal:state", data)
            else:
                with open(STATE_FILE, "w") as f:
                    f.write(data)
        except Exception as e:
            logger.error(f"save: {e}")

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
            self.cash = float(d.get("cash", STARTING_BANKROLL))
            self.peak_equity = float(d.get("peak_equity", self.cash))
            self.equity_curve = d.get("equity_curve", [])
            self.circuit = d.get("circuit", self.circuit)
        except Exception as e:
            logger.error(f"restore: {e}")

    # ── Equity (correct mark-to-market math) ────────────────────────────
    @property
    def open_cost(self) -> float:
        """$ deployed into open positions (at entry price)."""
        return sum(
            p.cost for p in self.positions.values()
            if p.status in ("open", "filled")
        )

    def open_market_value(self) -> float:
        """Mark-to-market value of all open positions."""
        total = 0.0
        for p in self.positions.values():
            if p.status not in ("open", "filled"):
                continue
            mark = p.current_price if p.current_price is not None else (p.fill_price or p.entry_price)
            total += p.size * mark
        return total

    @property
    def unrealized_pnl(self) -> float:
        return self.open_market_value() - self.open_cost

    @property
    def total_pnl(self) -> float:
        """Realized P&L from closed trades."""
        return sum(t.pnl for t in self.trades)

    @property
    def equity(self) -> float:
        """Cash + mark-to-market of opens. This is your actual wealth right now."""
        return self.cash + self.open_market_value()

    @property
    def total_equity(self) -> float:
        """Alias for equity. Kept for dashboard backward compat."""
        return self.equity

    def update_peak(self):
        eq = self.equity
        if eq > self.peak_equity:
            self.peak_equity = eq

    @property
    def drawdown_pct(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, (self.peak_equity - self.equity) / self.peak_equity)

    def record_equity_point(self):
        self.equity_curve.append([time.time(), round(self.equity, 2)])
        if len(self.equity_curve) > EQUITY_CURVE_MAX:
            self.equity_curve = self.equity_curve[-EQUITY_CURVE_MAX:]

    # ── Open / close ─────────────────────────────────────────────────────
    async def open_position(self, pos: Position):
        # Deduct cash at entry (we're paying to acquire shares)
        if pos.cost > self.cash + 0.01:
            logger.warning(f"Insufficient cash for {pos.id}: cost ${pos.cost:.2f} > cash ${self.cash:.2f}")
            # Cap to what we have, with $1 buffer
            affordable = max(0.0, self.cash - 1.0)
            if affordable < 1.0:
                return None
            scale = affordable / pos.cost
            pos.size = pos.size * scale
            pos.cost = affordable

        self.cash -= pos.cost
        self.positions[pos.id] = pos
        self.update_peak()
        await self._save()
        logger.info(
            f"Opened {pos.id}: {pos.engine} | {pos.team} @{pos.entry_price:.3f} "
            f"x{pos.size:.0f}sh cost=${pos.cost:.2f} cash=${self.cash:.2f}"
        )
        return pos

    async def mark_filled(self, pid: str, fill_price: Optional[float] = None):
        p = self.positions.get(pid)
        if not p:
            return
        p.status = "filled"
        p.filled_at = time.time()
        if fill_price is not None:
            # Adjust cash for fill-vs-expected slippage
            actual_cost = fill_price * p.size
            diff = actual_cost - p.cost
            self.cash -= diff
            p.fill_price = fill_price
            p.cost = actual_cost
        await self._save()

    async def cancel_position(self, pid: str):
        p = self.positions.get(pid)
        if not p:
            return
        # Refund cash
        self.cash += p.cost
        del self.positions[pid]
        await self._save()

    def mark_current_price(self, pid: str, price: float):
        """Update mark-to-market price without persisting (called every scan)."""
        p = self.positions.get(pid)
        if p:
            p.current_price = price
            p.last_mark_at = time.time()

    async def resolve_position(self, pid: str, winner_outcome: str, yes_price: float = None):
        """
        Resolve a position at game finish.
        winner_outcome: the outcome that won (either "YES"/"NO" for binary
        markets or the winning outcome name).
        """
        p = self.positions.get(pid)
        if not p:
            return

        if p.bet_is_yes_side:
            # Binary YES/NO market. p.bet_outcome == "YES"
            won = (winner_outcome == "YES") or (yes_price is not None and yes_price >= 0.99)
        else:
            # Named-outcome market. Compare outcome strings case-insensitively.
            from teams import teams_match
            won = teams_match(winner_outcome or "", p.bet_outcome or "")

        if winner_outcome in ("UNKNOWN", "", None):
            result, payout = "PUSH", p.cost
        elif won:
            result, payout = "WIN", p.size * 1.0
        else:
            result, payout = "LOSS", 0.0

        await self._close(p, result, payout, "resolution")

    async def exit_position(self, pid: str, exit_price: float, reason: str):
        p = self.positions.get(pid)
        if not p:
            return
        payout = exit_price * p.size
        result = "EXIT_PROFIT" if payout >= p.cost else "EXIT_LOSS"
        await self._close(p, result, payout, reason)

    async def partial_close(self, pid: str, exit_price: float, fraction: float, reason: str):
        """Sell `fraction` of position at exit_price. Keeps remainder open."""
        p = self.positions.get(pid)
        if not p or fraction <= 0 or fraction >= 1:
            return None
        shares_closing = p.size * fraction
        cost_closing = p.cost * fraction
        payout = shares_closing * exit_price
        pnl = payout - cost_closing

        p.size -= shares_closing
        p.cost -= cost_closing
        p.partial_exits += 1
        self.cash += payout

        trade = Trade(
            id=p.id, engine=p.engine, sport=p.sport,
            market_question=p.market_question,
            team=p.team, bet_outcome=p.bet_outcome,
            entry_price=p.fill_price or p.entry_price,
            exit_price=exit_price,
            size=shares_closing, cost=cost_closing,
            confidence=p.confidence,
            result="PARTIAL", payout=payout, pnl=pnl,
            pnl_pct=pnl / cost_closing if cost_closing > 0 else 0,
            opened_at=p.opened_at, closed_at=time.time(),
            score_line=p.score_line, true_prob=p.true_prob,
            edge_at_entry=p.edge_at_entry, exit_reason=reason,
            provider=getattr(p, 'provider', ''),
            entry_book_prob=_ml_to_prob_safe(getattr(p, 'moneyline', 0)),
            clv_prob=getattr(p, 'clv_prob', None),
            clv_edge=_calc_clv_edge(getattr(p, 'moneyline', 0), getattr(p, 'clv_prob', None)),
        )
        self.trades.append(trade)
        self.update_peak()
        await self._save()
        logger.info(f"[PARTIAL] {p.id}: {fraction*100:.0f}% @{exit_price:.3f} pnl=${pnl:+.2f} [{reason}]")
        return trade

    async def _close(self, p: Position, result: str, payout: float, reason: str):
        pnl = payout - p.cost
        pnl_pct = pnl / p.cost if p.cost > 0 else 0
        exit_price = payout / p.size if p.size > 0 else 0
        self.cash += payout

        self.trades.append(Trade(
            id=p.id, engine=p.engine, sport=p.sport,
            market_question=p.market_question,
            team=p.team, bet_outcome=p.bet_outcome,
            entry_price=p.fill_price or p.entry_price,
            exit_price=exit_price,
            size=p.size, cost=p.cost, confidence=p.confidence,
            result=result, payout=payout, pnl=pnl, pnl_pct=pnl_pct,
            opened_at=p.opened_at, closed_at=time.time(),
            score_line=p.score_line, true_prob=p.true_prob,
            edge_at_entry=p.edge_at_entry, exit_reason=reason,
            provider=getattr(p, 'provider', ''),
            entry_book_prob=_ml_to_prob_safe(getattr(p, 'moneyline', 0)),
            clv_prob=getattr(p, 'clv_prob', None),
            clv_edge=_calc_clv_edge(getattr(p, 'moneyline', 0), getattr(p, 'clv_prob', None)),
        ))
        del self.positions[p.id]

        # Circuit breaker bookkeeping
        if pnl > 0:
            self.circuit["consec_losses"] = 0
        else:
            self.circuit["consec_losses"] = self.circuit.get("consec_losses", 0) + 1

        self.update_peak()
        await self._save()
        sym = {"WIN":"[WIN]","LOSS":"[LOSS]","PUSH":"[PUSH]","EXIT_PROFIT":"[+]","EXIT_LOSS":"[-]"}.get(result, "[?]")
        logger.info(
            f"{sym} {p.id}: {result} ${pnl:+.2f} ({pnl_pct:+.1%}) [{reason}] "
            f"cash=${self.cash:.2f} equity=${self.equity:.2f}"
        )

    async def force_close(self, position_id: str, current_price: float, reason: str = "manual_close") -> Optional[Trade]:
        """Manually close a position at a given current price.
        Used by API /close endpoint. Returns the Trade or None if position not found.
        current_price should be the BID side (what we'd actually sell at).
        """
        p = self.positions.get(position_id)
        if not p or p.status not in ("open", "filled"):
            return None
        payout = p.size * current_price
        result = "EXIT_PROFIT" if payout > p.cost else "EXIT_LOSS"
        await self._close(p, result, payout, reason)
        return self.trades[-1] if self.trades else None

    # ── Queries ──────────────────────────────────────────────────────────
    def has_position_for(self, cid: str) -> bool:
        return any(p.condition_id == cid for p in self.positions.values() if p.status in ("open", "filled"))

    def has_position_for_game(self, home: str, away: str, engine: Optional[str] = None) -> bool:
        """Check if already positioned on this game (by either team), optionally filtered by engine."""
        from teams import teams_match
        for p in self.positions.values():
            if p.status not in ("open", "filled"):
                continue
            if engine and p.engine != engine:
                continue
            # We don't store home/away on positions. Match on score_line / market_question.
            text = f"{p.market_question} {p.score_line}".lower()
            if teams_match(home, p.team) or teams_match(away, p.team):
                return True
        return False

    def entries_for_game(self, home: str, away: str, engine: Optional[str] = None) -> int:
        """
        Count how many times we've OPENED a position on this game (including
        positions that have since closed). Used for re-entry logic: we allow
        up to 2 entries per game maximum.
        """
        from teams import teams_match
        count = 0
        # Count open positions
        for p in self.positions.values():
            if engine and p.engine != engine:
                continue
            if teams_match(home, p.team) or teams_match(away, p.team):
                count += 1
        # Count closed trades (different storage)
        for t in self.trades:
            if engine and t.engine != engine:
                continue
            if teams_match(home, t.team) or teams_match(away, t.team):
                count += 1
        return count

    def last_exit_time_for_game(self, home: str, away: str, engine: Optional[str] = None) -> Optional[float]:
        """Most recent exit timestamp for this game (for re-entry cooldown)."""
        from teams import teams_match
        latest = None
        for t in self.trades:
            if engine and t.engine != engine:
                continue
            if teams_match(home, t.team) or teams_match(away, t.team):
                ct = getattr(t, 'closed_at', 0)
                if latest is None or ct > latest:
                    latest = ct
        return latest

    def get_stale_orders(self, max_age_sec: float):
        now = time.time()
        return [p for p in self.positions.values() if p.status == "open" and (now - p.opened_at) > max_age_sec]

    def get_open_positions(self):
        return [p for p in self.positions.values() if p.status in ("open", "filled")]

    def get_filled_by_engine(self, engine: str):
        # Include both 'open' (pre-fill orders) and 'filled' (confirmed fills).
        # In paper mode, positions are marked 'filled' immediately, but positions
        # loaded from older state files may still be 'open' — they still need
        # exit processing.
        return [p for p in self.positions.values()
                if p.status in ("open", "filled") and p.engine == engine]

    def deployed_by_sport(self, sport: str) -> float:
        return sum(p.cost for p in self.get_open_positions() if p.sport == sport)

    def deployed_in_window(self, anchor_ts: Optional[float], window_hours: float = None) -> float:
        if anchor_ts is None:
            return 0.0
        window_hours = window_hours or CORRELATION_WINDOW_HOURS
        half = window_hours * 3600.0
        total = 0.0
        for p in self.get_open_positions():
            st_ts = self._parse_start_ts(p.game_start_time) or p.opened_at
            if st_ts is None:
                continue
            if abs(st_ts - anchor_ts) <= half:
                total += p.cost
        return total

    @staticmethod
    def _parse_start_ts(s: str) -> Optional[float]:
        if not s:
            return None
        try:
            if isinstance(s, (int, float)):
                return float(s)
            from datetime import datetime
            return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        except Exception:
            return None

    def deployed_by_engine(self, engine: str) -> float:
        return sum(p.cost for p in self.get_open_positions() if p.engine == engine)

    def stats(self, engine: Optional[str] = None) -> dict:
        trades = self.trades if not engine else [t for t in self.trades if t.engine == engine]
        # Don't count PARTIAL in win/loss counts
        scoring = [t for t in trades if t.result not in ("PARTIAL",)]
        positions = self.get_open_positions()
        if engine:
            positions = [p for p in positions if p.engine == engine]
        wins = [t for t in scoring if t.result in ("WIN", "EXIT_PROFIT")]
        losses = [t for t in scoring if t.result in ("LOSS", "EXIT_LOSS")]
        total = len(wins) + len(losses)
        return {
            "total_trades": len(scoring),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / total if total > 0 else 0.0,
            "total_pnl": sum(t.pnl for t in trades),  # include PARTIAL in $ pnl
            "avg_pnl": sum(t.pnl for t in trades) / len(trades) if trades else 0.0,
            "best_trade": max((t.pnl for t in trades), default=0.0),
            "worst_trade": min((t.pnl for t in trades), default=0.0),
            "open_positions": len(positions),
            "open_cost": sum(p.cost for p in positions),
            "unrealized": sum(
                (p.current_price if p.current_price is not None else p.entry_price) * p.size - p.cost
                for p in positions
            ),
        }

    # ── Circuit breaker ──────────────────────────────────────────────────
    def circuit_check(self) -> Tuple[bool, str]:
        """Return (allowed, reason). If not allowed, bot pauses new opens."""
        now = time.time()
        today = time.strftime("%Y-%m-%d", time.gmtime())
        c = self.circuit

        # Roll daily anchor
        if c.get("daily_date") != today:
            c["daily_date"] = today
            c["daily_anchor_equity"] = self.equity

        # In cooldown?
        if c.get("tripped") and now < c.get("until_ts", 0):
            return False, f"circuit cooldown until {int(c['until_ts'])}"
        if c.get("tripped") and now >= c.get("until_ts", 0):
            c["tripped"] = False
            c["consec_losses"] = 0

        # Consecutive losses
        if c.get("consec_losses", 0) >= CIRCUIT_CONSECUTIVE_LOSSES:
            c["tripped"] = True
            c["until_ts"] = now + CIRCUIT_COOLDOWN_MIN * 60
            return False, f"{CIRCUIT_CONSECUTIVE_LOSSES} consecutive losses"

        # Daily drawdown
        anchor = c.get("daily_anchor_equity") or self.equity
        if anchor > 0:
            daily_ret = (self.equity - anchor) / anchor
            if daily_ret <= -CIRCUIT_DAILY_LOSS_LIMIT:
                c["tripped"] = True
                c["until_ts"] = now + CIRCUIT_COOLDOWN_MIN * 60
                return False, f"daily drawdown {daily_ret*100:.1f}%"

        return True, "ok"
