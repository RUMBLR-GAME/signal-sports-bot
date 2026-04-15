"""
compound.py v2 — Dual-Engine Bankroll Manager

Changes from v1:
- Faster crypto resolution (configurable buffer, default 20s instead of 60s)
- Redis persistence across Railway deploys (optional, falls back to file)
- Archive included in state save for full recovery
"""

import json
import os
import time as _time
import requests
from datetime import datetime, timezone
from dataclasses import dataclass, asdict
from typing import Optional
import config


@dataclass
class Position:
    signal_id: str
    engine: str
    sport: str
    event: str
    outcome: str
    side: str
    token_id: str
    condition_id: str
    entry_price: float
    shares: int
    cost_basis: float
    confidence: float
    level: str
    detail: str
    entry_time: str
    status: str = "open"
    exit_price: float = 0.0
    pnl: float = 0.0
    resolved_time: str = ""


# ── Redis persistence (optional) ─────────────────
_redis_client = None

def _get_redis():
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    if not config.REDIS_URL:
        return None
    try:
        import redis
        _redis_client = redis.from_url(config.REDIS_URL, decode_responses=True)
        _redis_client.ping()
        print("  [PERSIST] Redis connected")
        return _redis_client
    except Exception as e:
        print(f"  [PERSIST] Redis unavailable: {e}")
        _redis_client = False  # Don't retry
        return None


class BankrollManager:
    def __init__(self):
        self.bankroll: float = config.STARTING_BANKROLL
        self.positions: list[Position] = []
        self.archive: list[dict] = []
        self.hwm: float = config.STARTING_BANKROLL
        self.total_trades: int = 0
        self.total_wins: int = 0
        self.total_pnl: float = 0.0
        self.harvest_count: int = 0
        self.synth_count: int = 0
        self._load()

    # ── Read-only getters ────────────────────────────
    def get_bankroll(self) -> float:
        return self.bankroll

    def get_equity(self) -> float:
        return self.bankroll + sum(p.cost_basis for p in self.positions if p.status == "open")

    def get_open(self) -> list[Position]:
        return [p for p in self.positions if p.status == "open"]

    def get_engine_exposure(self, engine: str) -> float:
        return sum(p.cost_basis for p in self.positions
                   if p.status == "open" and p.engine == engine)

    def get_total_exposure(self) -> float:
        return sum(p.cost_basis for p in self.positions if p.status == "open")

    def has_position(self, condition_id: str) -> bool:
        if not condition_id:
            return False
        return any(p.condition_id == condition_id and p.status == "open"
                   for p in self.positions)

    # ── Pre-trade validation ─────────────────────────
    def can_trade(self, cost: float, condition_id: str = "",
                  engine: str = "harvest") -> tuple[bool, str]:
        if condition_id and self.has_position(condition_id):
            return False, "Duplicate position"
        if cost <= 0:
            return False, "Zero cost"
        if cost > self.bankroll:
            return False, f"Need ${cost:.2f}, have ${self.bankroll:.2f}"

        eq = self.get_equity()
        if eq <= 0:
            return False, "Zero equity"

        total_exp = self.get_total_exposure()
        if (total_exp + cost) > eq * config.MAX_TOTAL_EXPOSURE_PCT:
            return False, f"Total exposure {(total_exp + cost)/eq:.0%} > {config.MAX_TOTAL_EXPOSURE_PCT:.0%}"

        eng_exp = self.get_engine_exposure(engine)
        if engine == "harvest":
            max_eng = eq * config.HARVEST_MAX_EXPOSURE_PCT
            max_usd = config.HARVEST_MAX_USD
        else:
            max_eng = eq * config.SYNTH_MAX_EXPOSURE_PCT
            max_usd = max(s.get("max_usd", 120) for s in config.SYNTH_SIZING.values())

        if eng_exp + cost > max_eng:
            return False, f"{engine} exposure {(eng_exp + cost)/eq:.0%} > limit"
        if cost > max_usd:
            return False, f"${cost:.0f} > ${max_usd:.0f} cap"

        return True, "OK"

    # ── Open position ────────────────────────────────
    def open_position(self, **kw) -> Position:
        cost = round(kw["shares"] * kw["entry_price"], 4)
        pos = Position(
            signal_id=kw["signal_id"], engine=kw["engine"],
            sport=kw["sport"], event=kw["event"], outcome=kw["outcome"],
            side=kw["side"], token_id=kw.get("token_id", ""),
            condition_id=kw["condition_id"], entry_price=kw["entry_price"],
            shares=kw["shares"], cost_basis=cost,
            confidence=kw["confidence"], level=kw["level"],
            detail=kw["detail"],
            entry_time=datetime.now(timezone.utc).isoformat(),
        )
        self.bankroll -= cost
        self.positions.append(pos)
        self.total_trades += 1
        if kw["engine"] == "harvest":
            self.harvest_count += 1
        else:
            self.synth_count += 1
        self._save()
        return pos

    # ── Close position ───────────────────────────────
    def _close(self, pos: Position, exit_price: float, status: str) -> Position:
        pos.status = status
        pos.exit_price = exit_price
        pos.pnl = round(pos.shares * exit_price - pos.cost_basis, 4)
        pos.resolved_time = datetime.now(timezone.utc).isoformat()
        self.bankroll += pos.shares * exit_price
        self.total_pnl += pos.pnl
        if pos.pnl > 0:
            self.total_wins += 1
        eq = self.get_equity()
        if eq > self.hwm:
            self.hwm = eq
        self._save()
        self._log_trade(pos)
        return pos

    # ── Resolution ───────────────────────────────────
    def check_resolutions(self, engine: str = "") -> list[Position]:
        resolved = []
        for pos in list(self.positions):
            if pos.status != "open":
                continue
            if engine and pos.engine != engine:
                continue

            if pos.engine == "synth":
                closed = self._resolve_crypto(pos)
            else:
                closed = self._resolve_harvest(pos)

            if closed:
                resolved.append(closed)

        self._archive_resolved()
        return resolved

    def _resolve_harvest(self, pos: Position) -> Optional[Position]:
        if not pos.condition_id:
            return None
        try:
            r = requests.get(f"{config.GAMMA_API}/markets",
                             params={"conditionId": pos.condition_id}, timeout=10)
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
        except Exception:
            return None

    def _resolve_crypto(self, pos: Position) -> Optional[Position]:
        now = _time.time()

        parts = pos.signal_id.rsplit("-", 1)
        try:
            window_ts = int(parts[-1])
        except (ValueError, IndexError):
            return None

        sid = pos.signal_id.lower()
        if "5m" in sid:
            duration = 300
        elif "15m" in sid:
            duration = 900
        elif "1h" in sid:
            duration = 3600
        else:
            duration = 3600

        close_time = window_ts + duration

        # Faster resolution: configurable buffer (default 20s instead of 60s)
        if now < close_time + config.CRYPTO_RESOLVE_BUFFER:
            return None

        # Handle arb positions — they always win if market resolves
        if pos.side == "BOTH":
            # Arb: we bought both sides, payout is always $1 per pair
            return self._close(pos, 1.0, "won")

        asset = pos.sport
        symbol = f"{asset}USDT"

        try:
            r1 = requests.get("https://api.binance.com/api/v3/klines",
                              params={"symbol": symbol, "interval": "1m",
                                      "startTime": window_ts * 1000, "limit": 1},
                              timeout=8)
            if not r1.ok or not r1.json():
                return None
            open_price = float(r1.json()[0][1])

            end_candle_start = (close_time - 60) * 1000
            r2 = requests.get("https://api.binance.com/api/v3/klines",
                              params={"symbol": symbol, "interval": "1m",
                                      "startTime": end_candle_start, "limit": 1},
                              timeout=8)
            if not r2.ok or not r2.json():
                return None
            close_price = float(r2.json()[0][4])

            actual_up = close_price >= open_price
            we_bet_up = pos.side == "YES"
            won = actual_up == we_bet_up

            return self._close(pos, 1.0 if won else 0.0, "won" if won else "lost")

        except Exception:
            return None

    def _parse_resolution(self, data: dict, side: str, outcome: str) -> Optional[float]:
        ps = data.get("outcomePrices", "")
        if isinstance(ps, str) and ps:
            try:
                prices = json.loads(ps)
                if len(prices) >= 2:
                    yes_p = float(prices[0])
                    no_p = float(prices[1])
                    if side == "YES":
                        if yes_p >= 0.99: return 1.0
                        if yes_p <= 0.01: return 0.0
                    elif side == "NO":
                        if no_p >= 0.99: return 1.0
                        if no_p <= 0.01: return 0.0
            except Exception:
                pass

        wo = data.get("winningOutcome", "").lower().strip()
        if not wo:
            return None
        ol = outcome.lower().strip()
        if side == "YES":
            if wo in ("yes", "1") or wo == ol:
                return 1.0
            if wo in ("no", "0"):
                return 0.0
        elif side == "NO":
            if wo in ("no", "0"):
                return 1.0
            if wo in ("yes", "1"):
                return 0.0
        if ol.startswith("not ") and wo in ("no", "0"):
            return 1.0
        return None

    # ── Position archival ────────────────────────────
    def _archive_resolved(self):
        still_active = []
        for pos in self.positions:
            if pos.status == "open":
                still_active.append(pos)
            else:
                self.archive.append(asdict(pos))
        if len(still_active) < len(self.positions):
            self.positions = still_active
            if len(self.archive) > 500:
                self.archive = self.archive[-500:]
            self._save()

    # ── Stats ────────────────────────────────────────
    def get_stats(self) -> dict:
        eq = self.get_equity()
        wr = self.total_wins / self.total_trades if self.total_trades > 0 else 0
        roi = (eq / config.STARTING_BANKROLL - 1) * 100 if config.STARTING_BANKROLL > 0 else 0
        dd = (1 - eq / self.hwm) * 100 if self.hwm > 0 else 0
        return {
            "bankroll": round(self.bankroll, 2), "equity": round(eq, 2),
            "starting": config.STARTING_BANKROLL, "pnl": round(self.total_pnl, 2),
            "roi": round(roi, 1), "trades": self.total_trades, "wins": self.total_wins,
            "win_rate": round(wr, 4), "hwm": round(self.hwm, 2),
            "open": len(self.get_open()),
            "exposure": round(self.get_total_exposure(), 2),
            "harvest_exposure": round(self.get_engine_exposure("harvest"), 2),
            "synth_exposure": round(self.get_engine_exposure("synth"), 2),
            "drawdown": round(dd, 1),
            "harvest_trades": self.harvest_count,
            "synth_trades": self.synth_count,
            "archived": len(self.archive),
        }

    # ── Persistence ──────────────────────────────────
    def _state_dict(self) -> dict:
        return {
            "bankroll": self.bankroll, "hwm": self.hwm,
            "total_trades": self.total_trades, "total_wins": self.total_wins,
            "total_pnl": self.total_pnl,
            "harvest_count": self.harvest_count, "synth_count": self.synth_count,
            "positions": [asdict(p) for p in self.positions],
            "archive": self.archive[-500:],  # Include archive for full recovery
            "archive_count": len(self.archive),
            "updated": datetime.now(timezone.utc).isoformat(),
        }

    def _save(self):
        state = self._state_dict()

        # Try Redis first (survives Railway redeploys)
        r = _get_redis()
        if r:
            try:
                r.set("signal_bot_state", json.dumps(state))
            except Exception as e:
                print(f"  [WARN] Redis save failed: {e}")

        # Always save to file too (local backup)
        try:
            os.makedirs(config.LOG_DIR, exist_ok=True)
            tmp = os.path.join(config.LOG_DIR, "state.tmp")
            path = os.path.join(config.LOG_DIR, "state.json")
            with open(tmp, "w") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp, path)
        except Exception as e:
            print(f"  [WARN] File save failed: {e}")

    def _load(self):
        state = None

        # Try Redis first
        r = _get_redis()
        if r:
            try:
                raw = r.get("signal_bot_state")
                if raw:
                    state = json.loads(raw)
                    print("  [PERSIST] Loaded state from Redis")
            except Exception as e:
                print(f"  [PERSIST] Redis load failed: {e}")

        # Fall back to file
        if not state:
            path = os.path.join(config.LOG_DIR, "state.json")
            if os.path.exists(path):
                try:
                    with open(path) as f:
                        state = json.load(f)
                    print("  [PERSIST] Loaded state from file")
                except Exception as e:
                    print(f"  [WARN] File load failed: {e}")

        if not state:
            return

        self.bankroll = state.get("bankroll", config.STARTING_BANKROLL)
        self.hwm = state.get("hwm", self.bankroll)
        self.total_trades = state.get("total_trades", 0)
        self.total_wins = state.get("total_wins", 0)
        self.total_pnl = state.get("total_pnl", 0.0)
        self.harvest_count = state.get("harvest_count", 0)
        self.synth_count = state.get("synth_count", 0)
        self.archive = state.get("archive", [])
        for pd in state.get("positions", []):
            for key in ("engine", "level", "detail"):
                pd.setdefault(key, "unknown")
            try:
                self.positions.append(Position(**pd))
            except (TypeError, ValueError):
                pass

    def _log_trade(self, pos: Position):
        try:
            os.makedirs(config.LOG_DIR, exist_ok=True)
            entry = {
                **asdict(pos),
                "bankroll_after": round(self.bankroll, 2),
                "equity_after": round(self.get_equity(), 2),
                "total_pnl": round(self.total_pnl, 2),
            }
            with open(config.TRADE_LOG, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass
