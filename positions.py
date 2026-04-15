"""
positions.py — Position & State Manager

Tracks all open and resolved positions.
Persists to Redis (survives deploys) + file (local backup).
Fee-aware P&L calculations.

Sports resolution via Polymarket Gamma API (checks if market resolved).
"""

import json
import os
import time
import logging
import requests
from datetime import datetime, timezone
from dataclasses import dataclass, asdict, field
from typing import Optional

import config

logger = logging.getLogger("positions")

# ── Sports taker fee (only relevant if we accidentally take) ──
# Maker = 0%, so our P&L calc is: pnl = shares × exit_price - cost_basis
# No fee deduction needed for maker orders.
SPORTS_TAKER_FEE = 0.0075  # 0.75% — only used for worst-case P&L estimates


@dataclass
class Position:
    signal_id: str
    sport: str
    event: str
    outcome: str
    side: str                  # "YES" or "NO"
    token_id: str
    condition_id: str
    entry_price: float
    shares: int
    cost_basis: float          # shares × entry_price
    confidence: float
    level: str
    detail: str                # score line at entry
    ev_at_entry: float         # EV per share at entry
    price_source: str          # "clob" or "gamma"
    spread_at_entry: float
    order_id: str = ""         # CLOB order ID (empty for paper)
    entry_time: str = ""
    # Resolution
    status: str = "open"       # open / filled / won / lost / cancelled
    exit_price: float = 0.0
    pnl: float = 0.0
    resolved_time: str = ""


class PositionManager:
    """Thread-safe position tracking with persistence."""

    def __init__(self):
        self.positions: list[Position] = []
        self.history: list[dict] = []  # resolved trades (dicts for compactness)
        self.total_trades: int = 0
        self.total_wins: int = 0
        self.total_pnl: float = 0.0
        self._load()

    # ── Queries ──────────────────────────────────
    def get_open(self) -> list[Position]:
        return [p for p in self.positions if p.status in ("open", "filled")]

    def get_exposure(self) -> float:
        return sum(p.cost_basis for p in self.get_open())

    def has_position(self, condition_id: str) -> bool:
        if not condition_id:
            return False
        return any(
            p.condition_id == condition_id and p.status in ("open", "filled")
            for p in self.positions
        )

    def get_equity(self, bankroll: float) -> float:
        """Cash + cost basis of open positions."""
        return bankroll + self.get_exposure()

    # ── Open ─────────────────────────────────────
    def open(self, sig, order_id: str = "") -> Position:
        """Record a new position from a HarvestSignal."""
        pos = Position(
            signal_id=sig.id,
            sport=sig.sport,
            event=sig.event_title,
            outcome=sig.outcome,
            side=sig.side,
            token_id=sig.token_id,
            condition_id=sig.condition_id,
            entry_price=sig.price,
            shares=sig.shares,
            cost_basis=round(sig.shares * sig.price, 4),
            confidence=sig.confidence,
            level=sig.level,
            detail=sig.score_line,
            ev_at_entry=sig.ev_per_share,
            price_source=sig.price_source,
            spread_at_entry=sig.spread,
            order_id=order_id,
            entry_time=datetime.now(timezone.utc).isoformat(),
            status="filled" if order_id or config.PAPER_MODE else "open",
        )
        self.positions.append(pos)
        self.total_trades += 1
        self._save()
        return pos

    # ── Close ────────────────────────────────────
    def _close(self, pos: Position, exit_price: float, status: str) -> Position:
        pos.status = status
        pos.exit_price = exit_price
        # Maker orders = zero fees, so pure P&L
        pos.pnl = round(pos.shares * exit_price - pos.cost_basis, 4)
        pos.resolved_time = datetime.now(timezone.utc).isoformat()
        self.total_pnl += pos.pnl
        if pos.pnl > 0:
            self.total_wins += 1
        self._save()
        self._log_trade(pos)
        logger.info(
            f"{'✓' if pos.pnl > 0 else '✗'} {pos.event} → "
            f"${pos.pnl:+.2f} ({status}) exit@{exit_price}"
        )
        return pos

    def cancel(self, pos: Position):
        """Mark a position as cancelled (order didn't fill)."""
        pos.status = "cancelled"
        self.total_trades -= 1  # Don't count unfilled orders
        self._save()

    # ── Resolution ───────────────────────────────
    def check_resolutions(self) -> list[Position]:
        """Check Polymarket for resolved markets. Returns newly resolved positions."""
        resolved = []
        for pos in list(self.positions):
            if pos.status not in ("open", "filled"):
                continue

            result = self._check_resolution(pos)
            if result is not None:
                resolved.append(result)

        self._archive_resolved()
        return resolved

    def _check_resolution(self, pos: Position) -> Optional[Position]:
        """Query Gamma API for market resolution."""
        if not pos.condition_id:
            return None
        try:
            r = requests.get(
                f"{config.GAMMA_API}/markets",
                params={"conditionId": pos.condition_id},
                timeout=10,
            )
            if not r.ok:
                return None
            results = r.json()
            if not results:
                return None
            data = results[0] if isinstance(results, list) else results

            if not (data.get("closed") or data.get("resolved")):
                return None

            exit_price = self._parse_resolution(data, pos.side, pos.outcome)
            if exit_price is None:
                return None

            return self._close(pos, exit_price, "won" if exit_price > 0.5 else "lost")

        except Exception as e:
            logger.error(f"Resolution check error: {e}")
            return None

    def _parse_resolution(self, data: dict, side: str, outcome: str) -> Optional[float]:
        """Parse market resolution to determine exit price."""
        # Method 1: outcomePrices
        ps = data.get("outcomePrices", "")
        if isinstance(ps, str) and ps:
            try:
                prices = json.loads(ps)
                if len(prices) >= 2:
                    yes_p, no_p = float(prices[0]), float(prices[1])
                    if side == "YES":
                        if yes_p >= 0.99: return 1.0
                        if yes_p <= 0.01: return 0.0
                    elif side == "NO":
                        if no_p >= 0.99: return 1.0
                        if no_p <= 0.01: return 0.0
            except Exception:
                pass

        # Method 2: winningOutcome
        wo = data.get("winningOutcome", "").lower().strip()
        if not wo:
            return None
        ol = outcome.lower().strip()
        if side == "YES":
            if wo in ("yes", "1") or wo == ol: return 1.0
            if wo in ("no", "0"): return 0.0
        elif side == "NO":
            if wo in ("no", "0"): return 1.0
            if wo in ("yes", "1"): return 0.0
        if ol.startswith("not ") and wo in ("no", "0"):
            return 1.0
        return None

    # ── Archival ─────────────────────────────────
    def _archive_resolved(self):
        active = []
        for pos in self.positions:
            if pos.status in ("open", "filled"):
                active.append(pos)
            elif pos.status in ("won", "lost"):
                self.history.append(asdict(pos))
            # cancelled positions just get dropped
        if len(active) < len(self.positions):
            self.positions = active
            if len(self.history) > 1000:
                self.history = self.history[-1000:]
            self._save()

    # ── Stats ────────────────────────────────────
    def get_stats(self, bankroll: float) -> dict:
        eq = self.get_equity(bankroll)
        wr = self.total_wins / self.total_trades if self.total_trades > 0 else 0
        roi = (eq / config.STARTING_BANKROLL - 1) * 100 if config.STARTING_BANKROLL > 0 else 0
        open_pos = self.get_open()
        return {
            "equity": round(eq, 2),
            "bankroll": round(bankroll, 2),
            "starting": config.STARTING_BANKROLL,
            "pnl": round(self.total_pnl, 2),
            "roi": round(roi, 1),
            "trades": self.total_trades,
            "wins": self.total_wins,
            "win_rate": round(wr, 4),
            "open": len(open_pos),
            "exposure": round(self.get_exposure(), 2),
            "mode": "PAPER" if config.PAPER_MODE else "LIVE",
        }

    # ── Persistence ──────────────────────────────
    def _state_dict(self) -> dict:
        return {
            "total_trades": self.total_trades,
            "total_wins": self.total_wins,
            "total_pnl": self.total_pnl,
            "positions": [asdict(p) for p in self.positions],
            "history": self.history[-1000:],
            "updated": datetime.now(timezone.utc).isoformat(),
        }

    def _save(self):
        state = self._state_dict()

        # Redis (survives Railway redeploys)
        if config.REDIS_URL:
            try:
                import redis
                r = redis.from_url(config.REDIS_URL, decode_responses=True)
                r.set("signal_state", json.dumps(state))
            except Exception as e:
                logger.warning(f"Redis save failed: {e}")

        # File (local backup)
        try:
            os.makedirs(os.path.dirname(config.STATE_FILE) or ".", exist_ok=True)
            tmp = config.STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp, config.STATE_FILE)
        except Exception as e:
            logger.warning(f"File save failed: {e}")

    def _load(self):
        state = None

        # Try Redis first
        if config.REDIS_URL:
            try:
                import redis
                r = redis.from_url(config.REDIS_URL, decode_responses=True)
                raw = r.get("signal_state")
                if raw:
                    state = json.loads(raw)
                    logger.info("Loaded state from Redis")
            except Exception:
                pass

        # Fall back to file
        if not state and os.path.exists(config.STATE_FILE):
            try:
                with open(config.STATE_FILE) as f:
                    state = json.load(f)
                logger.info("Loaded state from file")
            except Exception:
                pass

        if not state:
            return

        self.total_trades = state.get("total_trades", 0)
        self.total_wins = state.get("total_wins", 0)
        self.total_pnl = state.get("total_pnl", 0.0)
        self.history = state.get("history", [])
        for pd in state.get("positions", []):
            # Defaults for fields that may not exist in old state
            defaults = {
                "level": "unknown", "detail": "", "ev_at_entry": 0.0,
                "price_source": "unknown", "spread_at_entry": 0.0,
                "order_id": "", "status": "open", "exit_price": 0.0,
                "pnl": 0.0, "resolved_time": "",
            }
            for key, default in defaults.items():
                pd.setdefault(key, default)
            try:
                self.positions.append(Position(**pd))
            except (TypeError, ValueError):
                pass

    def _log_trade(self, pos: Position):
        try:
            os.makedirs(os.path.dirname(config.TRADE_LOG) or ".", exist_ok=True)
            entry = {**asdict(pos), "total_pnl": round(self.total_pnl, 2)}
            with open(config.TRADE_LOG, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass
