"""
crypto.py v2 — Three-Layer Crypto Prediction Market Engine

LAYER 1: LATENCY SNIPE (98% win rate)
  At T-15s before market close, check if BTC has already moved decisively.
  Query REAL CLOB price. Only trade when EV is positive.
  Kelly criterion for position sizing.

LAYER 2: SYNTH EDGE (60% win rate, 10%+ edge)
  When Synth SN50 probability diverges from Polymarket by >5%.

LAYER 3: PAIR ARBITRAGE (100% win rate, small return)
  When Up + Down < $0.97, buy both sides for risk-free profit.

Smart scan timing: sleeps until T-20s before next window close,
then scans every 3s during the hot zone. Never misses a window.
"""

import time
import json
import math
import threading
import logging
import requests
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional
import config
from polymarket_prices import get_real_price, check_arb

logger = logging.getLogger("crypto")

# ── Shared price state from Binance ──────────────
_btc_price = {"price": 0.0, "time": 0.0}
_eth_price = {"price": 0.0, "time": 0.0}
_price_lock = threading.Lock()


def get_btc_price() -> float:
    with _price_lock:
        return _btc_price["price"]

def get_eth_price() -> float:
    with _price_lock:
        return _eth_price["price"]


def start_binance_feed():
    """Start price feeds for real-time BTC and ETH prices."""
    def _poll_loop():
        while True:
            try:
                r = requests.get("https://api.binance.com/api/v3/ticker/price",
                                 params={"symbol": "BTCUSDT"}, timeout=5)
                if r.ok:
                    with _price_lock:
                        _btc_price["price"] = float(r.json().get("price", 0))
                        _btc_price["time"] = time.time()
            except Exception:
                pass
            try:
                r2 = requests.get("https://api.binance.com/api/v3/ticker/price",
                                  params={"symbol": "ETHUSDT"}, timeout=5)
                if r2.ok:
                    with _price_lock:
                        _eth_price["price"] = float(r2.json().get("price", 0))
                        _eth_price["time"] = time.time()
            except Exception:
                pass
            time.sleep(3)

    def _price_cache_loop():
        while True:
            try:
                now = time.time()
                next_5m = (int(now) // 300 + 1) * 300
                sleep_until = next_5m - now + 2
                if sleep_until > 0:
                    time.sleep(min(sleep_until, 30))
                for asset in ["BTC", "ETH"]:
                    window_ts = int(time.time()) - (int(time.time()) % 300)
                    _get_window_open_price(window_ts, asset)
                    window_ts_15 = int(time.time()) - (int(time.time()) % 900)
                    _get_window_open_price(window_ts_15, asset)
            except Exception:
                time.sleep(10)

    try:
        import websocket
        def _ws_loop(symbol, state):
            url = f"wss://stream.binance.com:9443/ws/{symbol}@ticker"
            while True:
                try:
                    ws = websocket.WebSocket()
                    ws.connect(url, timeout=10)
                    while True:
                        msg = ws.recv()
                        data = json.loads(msg)
                        with _price_lock:
                            state["price"] = float(data.get("c", 0))
                            state["time"] = time.time()
                except Exception:
                    time.sleep(3)
        threading.Thread(target=_ws_loop, args=("btcusdt", _btc_price), daemon=True).start()
        threading.Thread(target=_ws_loop, args=("ethusdt", _eth_price), daemon=True).start()
        print("  [CRYPTO] Binance WebSocket started (BTC + ETH)")
    except Exception:
        threading.Thread(target=_poll_loop, daemon=True).start()
        print("  [CRYPTO] Binance REST polling started (BTC + ETH)")

    threading.Thread(target=_poll_loop, daemon=True).start()
    threading.Thread(target=_price_cache_loop, daemon=True).start()
    return True


@dataclass
class CryptoSignal:
    """A signal from the crypto engine."""
    id: str
    asset: str
    timeframe: str
    layer: str                    # "snipe", "synth", "arb"
    direction: str
    price: float
    side: str
    shares: int
    cost: float
    implied_return: float
    window_delta_pct: float
    synth_prob_up: float
    poly_prob_up: float
    edge: float
    confidence: float
    slug: str
    event_end: str
    seconds_remaining: int
    detail: str
    timestamp: str
    # Real price data
    price_source: str = "estimate"
    real_price: float = 0.0
    midpoint: float = 0.0
    spread: float = 0.0
    token_id: str = ""
    ev: float = 0.0               # Expected value per share


# ── Kelly sizing ─────────────────────────────────
def _kelly_size(win_prob: float, price: float, equity: float,
                max_pct: float, max_usd: float) -> int:
    """Kelly criterion position sizing.
    
    For binary markets: bet resolves to $1 if win, $0 if loss.
    Kelly fraction = (win_prob * payout - loss_prob) / payout
    where payout = (1 - price) / price  (net gain per dollar risked)
    
    We use quarter-Kelly for safety.
    """
    if price <= 0 or price >= 1 or win_prob <= price:
        return 0

    payout = (1.0 - price) / price  # Net gain per $1 risked
    loss_prob = 1.0 - win_prob

    kelly = (win_prob * payout - loss_prob) / payout
    kelly = max(kelly, 0)

    # Quarter-Kelly with hard caps
    bet_fraction = kelly * config.KELLY_FRACTION
    bet_fraction = min(bet_fraction, config.KELLY_MAX_BET_PCT, max_pct)

    pos_usd = min(equity * bet_fraction, max_usd)
    shares = int(pos_usd / price)

    return shares if shares >= config.MIN_SHARES else 0


# ── Smart scan timing ────────────────────────────
def get_next_scan_delay() -> float:
    """Calculate optimal sleep time until next trading window.
    
    Instead of fixed 15s intervals (which miss windows), we calculate
    exactly when the next 5-min and 15-min windows close and wake up
    at T-20s before the closest one.
    """
    now = time.time()

    # Next 5-min close
    window_5m = int(now) - (int(now) % 300)
    close_5m = window_5m + 300

    # Next 15-min close
    window_15m = int(now) - (int(now) % 900)
    close_15m = window_15m + 900

    # How far are we from each close?
    secs_to_5m = close_5m - now
    secs_to_15m = close_15m - now

    lead = config.SNIPE_LEAD_TIME  # 20s

    # If we're already in a hot zone (< lead seconds to any close), scan fast
    if secs_to_5m <= lead or secs_to_15m <= lead:
        return config.SNIPE_SCAN_BURST  # 3s

    # Otherwise sleep until the soonest hot zone
    next_hot = min(secs_to_5m, secs_to_15m) - lead
    # Cap at 30s so we don't miss anything weird
    return min(max(next_hot, 1.0), 30.0)


def scan_crypto_opportunities(equity: float, open_crypto_exposure: float) -> list[CryptoSignal]:
    """Main scanner — all three layers."""
    signals = []
    now = time.time()
    max_exposure = equity * config.SYNTH_MAX_EXPOSURE_PCT
    remaining = max_exposure - open_crypto_exposure
    if remaining <= 0:
        return []

    btc = get_btc_price()
    eth = get_eth_price()
    if btc <= 0:
        return []

    # LAYER 1: Latency snipes
    for asset, price in [("BTC", btc), ("ETH", eth)]:
        if price <= 0:
            continue
        sig = _check_5min_market(price, asset, now, equity, remaining)
        if sig:
            signals.append(sig)
            remaining -= sig.cost

    for asset, price in [("BTC", btc), ("ETH", eth)]:
        if price <= 0:
            continue
        sig = _check_15min_market(price, asset, now, equity, remaining)
        if sig:
            signals.append(sig)
            remaining -= sig.cost

    # LAYER 2: Synth hourly
    if config.SYNTH_API_KEY:
        for asset in config.SYNTH_ASSETS:
            sig = _check_synth_hourly(asset, equity, remaining)
            if sig:
                signals.append(sig)
                remaining -= sig.cost

    # LAYER 3: Pair arbitrage
    if config.ARB_ENABLED:
        for asset in ["BTC", "ETH"]:
            for window in [5, 15]:
                sig = _check_arb(asset, window, now, equity, remaining)
                if sig:
                    signals.append(sig)
                    remaining -= sig.cost

    return signals


def _check_5min_market(price_now: float, asset: str, now: float,
                        equity: float, remaining: float) -> Optional[CryptoSignal]:
    """LAYER 1: 5-minute latency snipe with EV-based decision and Kelly sizing."""
    window_ts = int(now) - (int(now) % 300)
    close_time = window_ts + 300
    seconds_left = close_time - now

    if seconds_left > 15 or seconds_left < 2:
        return None

    open_price = _get_window_open_price(window_ts, asset)
    if open_price <= 0:
        return None

    delta_pct = (price_now - open_price) / open_price * 100
    abs_delta = abs(delta_pct)

    if abs_delta < 0.05:
        return None

    direction = "up" if delta_pct > 0 else "down"
    slug_prefix = "btc" if asset == "BTC" else "eth"
    slug = f"{slug_prefix}-updown-5m-{window_ts}"

    # Confidence from delta magnitude
    if abs_delta >= 0.15:
        confidence = 0.98
    elif abs_delta >= 0.10:
        confidence = 0.97
    elif abs_delta >= 0.05:
        confidence = 0.88
    else:
        confidence = 0.70

    # ── Get real CLOB price ──
    real = get_real_price(slug, direction)

    if real is not None:
        buy_price = real["buy_price"]
        midpoint_val = real["midpoint"]
        spread_val = real["spread"]
        token_id = real["token_id"]
        price_source = "clob"

        # EV calculation: expected value per share
        # EV = (confidence × $1.00) + ((1-confidence) × $0.00) - buy_price
        ev_per_share = confidence * 1.0 - buy_price

        # Gate: must have positive EV above minimum edge
        if ev_per_share < config.KELLY_MIN_EDGE:
            logger.info(f"[5m] {asset} EV too low ({ev_per_share:.4f}), skipping")
            return None

        if spread_val > 0.10:
            logger.info(f"[5m] {asset} spread too wide ({spread_val:.4f}), skipping")
            return None

        if buy_price >= 0.98:
            logger.info(f"[5m] {asset} price too high ({buy_price:.4f}), no room")
            return None

        est_price = buy_price
    else:
        logger.warning(f"[5m] CLOB unavailable for {slug}, using estimate")
        midpoint_val = 0.0
        spread_val = 0.0
        token_id = ""
        price_source = "estimate"
        ev_per_share = 0.0

        if abs_delta >= 0.15:
            est_price = 0.92
        elif abs_delta >= 0.10:
            est_price = 0.85
        elif abs_delta >= 0.05:
            est_price = 0.72
        else:
            return None

        ev_per_share = confidence - est_price

    if est_price < 0.40:
        return None

    # Kelly sizing (uses 5min config now, not 15min)
    sizing = config.SYNTH_SIZING.get("5min", {})
    shares = _kelly_size(
        win_prob=confidence, price=est_price, equity=equity,
        max_pct=sizing.get("pct", 0.06), max_usd=min(sizing.get("max_usd", 80), remaining),
    )
    if shares < config.MIN_SHARES:
        return None

    cost = round(shares * est_price, 2)
    implied_return = (1.0 - est_price) / est_price
    now_dt = datetime.now(timezone.utc)

    source_tag = "LIVE" if price_source == "clob" else "EST"

    return CryptoSignal(
        id=f"snipe-{asset}-5m-{window_ts}",
        asset=asset, timeframe="5min", layer="snipe",
        direction=direction, price=est_price,
        side="YES" if direction == "up" else "NO",
        shares=shares, cost=cost,
        implied_return=round(implied_return, 4),
        window_delta_pct=round(delta_pct, 4),
        synth_prob_up=0, poly_prob_up=0,
        edge=round(ev_per_share, 4),
        confidence=confidence,
        slug=slug, event_end=datetime.fromtimestamp(close_time, timezone.utc).isoformat(),
        seconds_remaining=int(seconds_left),
        detail=f"SNIPE {asset} Δ{delta_pct:+.3f}% T-{int(seconds_left)}s → {direction.upper()} @ ${est_price:.4f} [{source_tag}] EV:{ev_per_share:+.4f}",
        timestamp=now_dt.isoformat(),
        price_source=price_source,
        real_price=est_price if price_source == "clob" else 0.0,
        midpoint=midpoint_val,
        spread=spread_val,
        token_id=token_id,
        ev=round(ev_per_share, 4),
    )


def _check_15min_market(price_now: float, asset: str, now: float,
                         equity: float, remaining: float) -> Optional[CryptoSignal]:
    """LAYER 1: 15-minute latency snipe with EV and Kelly."""
    window_ts = int(now) - (int(now) % 900)
    close_time = window_ts + 900
    seconds_left = close_time - now

    if seconds_left > 30 or seconds_left < 3:
        return None

    open_price = _get_window_open_price(window_ts, asset)
    if open_price <= 0:
        return None

    delta_pct = (price_now - open_price) / open_price * 100
    abs_delta = abs(delta_pct)

    if abs_delta < 0.08:
        return None

    direction = "up" if delta_pct > 0 else "down"
    slug_prefix = "btc" if asset == "BTC" else "eth"
    slug = f"{slug_prefix}-updown-15m-{window_ts}"

    confidence = min(0.75 + abs_delta * 1.5, 0.98)

    # ── Get real CLOB price ──
    real = get_real_price(slug, direction)

    if real is not None:
        buy_price = real["buy_price"]
        midpoint_val = real["midpoint"]
        spread_val = real["spread"]
        token_id = real["token_id"]
        price_source = "clob"

        ev_per_share = confidence * 1.0 - buy_price

        if ev_per_share < config.KELLY_MIN_EDGE:
            logger.info(f"[15m] {asset} EV too low ({ev_per_share:.4f}), skipping")
            return None

        if spread_val > 0.10:
            logger.info(f"[15m] {asset} spread too wide ({spread_val:.4f}), skipping")
            return None

        if buy_price >= 0.98:
            return None

        est_price = buy_price
    else:
        logger.warning(f"[15m] CLOB unavailable for {slug}, using estimate")
        midpoint_val = 0.0
        spread_val = 0.0
        token_id = ""
        price_source = "estimate"

        if abs_delta >= 0.25:
            est_price = 0.93
        elif abs_delta >= 0.15:
            est_price = 0.85
        elif abs_delta >= 0.08:
            est_price = 0.75
        else:
            return None

        ev_per_share = confidence - est_price

    if est_price < 0.40:
        return None

    sizing = config.SYNTH_SIZING.get("15min", {})
    shares = _kelly_size(
        win_prob=confidence, price=est_price, equity=equity,
        max_pct=sizing.get("pct", 0.04), max_usd=min(sizing.get("max_usd", 60), remaining),
    )
    if shares < config.MIN_SHARES:
        return None

    cost = round(shares * est_price, 2)
    implied_return = (1.0 - est_price) / est_price
    source_tag = "LIVE" if price_source == "clob" else "EST"

    return CryptoSignal(
        id=f"snipe-{asset}-15m-{window_ts}",
        asset=asset, timeframe="15min", layer="snipe",
        direction=direction, price=est_price,
        side="YES" if direction == "up" else "NO",
        shares=shares, cost=cost,
        implied_return=round(implied_return, 4),
        window_delta_pct=round(delta_pct, 4),
        synth_prob_up=0, poly_prob_up=0,
        edge=round(ev_per_share, 4),
        confidence=confidence,
        slug=slug, event_end=datetime.fromtimestamp(close_time, timezone.utc).isoformat(),
        seconds_remaining=int(seconds_left),
        detail=f"SNIPE {asset} 15m Δ{delta_pct:+.3f}% T-{int(seconds_left)}s → {direction.upper()} @ ${est_price:.4f} [{source_tag}] EV:{ev_per_share:+.4f}",
        timestamp=datetime.now(timezone.utc).isoformat(),
        price_source=price_source,
        real_price=est_price if price_source == "clob" else 0.0,
        midpoint=midpoint_val,
        spread=spread_val,
        token_id=token_id,
        ev=round(ev_per_share, 4),
    )


def _check_arb(asset: str, window_min: int, now: float,
                equity: float, remaining: float) -> Optional[CryptoSignal]:
    """LAYER 3: Pair arbitrage — buy both sides when combined < threshold."""
    period = window_min * 60
    window_ts = int(now) - (int(now) % period)
    close_time = window_ts + period
    seconds_left = close_time - now

    # Only arb with enough time for execution (but not too early — need liquid book)
    if seconds_left > 120 or seconds_left < 10:
        return None

    slug_prefix = "btc" if asset == "BTC" else "eth"
    slug = f"{slug_prefix}-updown-{window_min}m-{window_ts}"

    arb = check_arb(slug)
    if not arb:
        return None

    if arb["combined"] >= config.ARB_MAX_COMBINED:
        return None

    profit_per_pair = arb["profit_per_share"]
    if profit_per_pair < 0.01:
        return None  # Not worth the execution risk

    # Size: buy equal shares of both sides
    pair_cost = arb["combined"]
    max_usd = min(config.ARB_MAX_USD, remaining)
    pairs = int(max_usd / pair_cost)
    if pairs < config.MIN_SHARES:
        return None

    cost = round(pairs * pair_cost, 2)
    total_profit = round(pairs * profit_per_pair, 2)

    logger.info(
        f"ARB {asset} {window_min}m: {pairs} pairs @ ${pair_cost:.4f} "
        f"→ ${total_profit:.2f} profit ({profit_per_pair/pair_cost*100:.1f}%)"
    )

    return CryptoSignal(
        id=f"arb-{asset}-{window_min}m-{window_ts}",
        asset=asset, timeframe=f"{window_min}min", layer="arb",
        direction="both", price=pair_cost,
        side="BOTH",
        shares=pairs, cost=cost,
        implied_return=round(profit_per_pair / pair_cost, 4),
        window_delta_pct=0,
        synth_prob_up=0, poly_prob_up=0,
        edge=round(profit_per_pair, 4),
        confidence=1.0,
        slug=slug, event_end=datetime.fromtimestamp(close_time, timezone.utc).isoformat(),
        seconds_remaining=int(seconds_left),
        detail=f"ARB {asset} {window_min}m: {pairs}× YES@${arb['yes_price']:.4f} + NO@${arb['no_price']:.4f} = ${pair_cost:.4f} → +${total_profit:.2f}",
        timestamp=datetime.now(timezone.utc).isoformat(),
        price_source="clob",
        real_price=pair_cost,
        midpoint=0,
        spread=0,
        token_id=arb["yes_token"],
        ev=round(profit_per_pair, 4),
    )


def _check_synth_hourly(asset: str, equity: float, remaining: float) -> Optional[CryptoSignal]:
    """LAYER 2: Synth probability edge on hourly markets."""
    if not config.SYNTH_API_KEY:
        return None

    try:
        r = requests.get(
            f"{config.SYNTH_BASE}/insights/polymarket/up-down/hourly",
            headers={"Authorization": f"Apikey {config.SYNTH_API_KEY}"},
            params={"asset": asset},
            timeout=10,
        )
        if not r.ok:
            return None
        data = r.json()
        if not data or "error" in data:
            return None
    except Exception:
        return None

    synth_up = data.get("synth_probability_up")
    poly_up = data.get("polymarket_probability_up")
    if synth_up is None or poly_up is None:
        return None

    edge_up = synth_up - poly_up
    sizing = config.SYNTH_SIZING.get("hourly", {})
    min_edge = sizing.get("min_edge", 0.07)

    if abs(edge_up) < min_edge:
        return None

    if edge_up > 0:
        direction = "up"
        edge = edge_up
        price = data.get("best_ask_price", poly_up) or poly_up
        side = "YES"
        win_prob = synth_up
    else:
        direction = "down"
        edge = abs(edge_up)
        bid = data.get("best_bid_price", poly_up)
        price = (1 - bid) if bid else (1 - poly_up)
        side = "NO"
        win_prob = 1 - synth_up

    if price < 0.10 or price > 0.92:
        return None

    # Kelly sizing for synth too
    shares = _kelly_size(
        win_prob=win_prob, price=price, equity=equity,
        max_pct=sizing.get("pct", 0.06), max_usd=min(sizing.get("max_usd", 100), remaining),
    )
    if shares < config.MIN_SHARES:
        return None

    cost = round(shares * price, 2)
    implied_return = (1.0 - price) / price
    ev_per_share = win_prob - price
    confidence_label = "strong" if edge >= 0.12 else "moderate" if edge >= 0.08 else "weak"

    return CryptoSignal(
        id=f"synth-{asset}-1h-{int(time.time())}",
        asset=asset, timeframe="hourly", layer="synth",
        direction=direction, price=round(price, 4),
        side=side, shares=shares, cost=cost,
        implied_return=round(implied_return, 4),
        window_delta_pct=0,
        synth_prob_up=round(synth_up, 4),
        poly_prob_up=round(poly_up, 4),
        edge=round(edge, 4),
        confidence=round(win_prob, 4),
        slug=data.get("slug", ""),
        event_end=data.get("event_end_time", ""),
        seconds_remaining=0,
        detail=f"SYNTH {asset} 1h {direction.upper()} │ Synth:{synth_up:.1%} vs Poly:{poly_up:.1%} = {edge:+.1%} edge │ {confidence_label}",
        timestamp=datetime.now(timezone.utc).isoformat(),
        price_source="synth",
        ev=round(ev_per_share, 4),
    )


# ── Window open price tracking ───────────────────
_window_opens = {}
_window_lock = threading.Lock()


def _get_window_open_price(window_ts: int, asset: str) -> float:
    key = (asset, window_ts)
    with _window_lock:
        if key in _window_opens:
            return _window_opens[key]

    symbol = f"{asset}USDT"
    try:
        r = requests.get("https://api.binance.com/api/v3/klines",
                         params={"symbol": symbol, "interval": "1m",
                                 "startTime": window_ts * 1000, "limit": 1},
                         timeout=5)
        if r.ok:
            data = r.json()
            if data:
                open_price = float(data[0][1])
                with _window_lock:
                    _window_opens[key] = open_price
                    if len(_window_opens) > 50:
                        oldest = sorted(_window_opens.keys(), key=lambda k: k[1])[:20]
                        for k in oldest:
                            del _window_opens[k]
                return open_price
    except Exception:
        pass

    if asset == "BTC":
        return get_btc_price()
    elif asset == "ETH":
        return get_eth_price()
    return 0.0


def get_synth_status() -> dict:
    if not config.SYNTH_API_KEY:
        return {"connected": False, "message": "No SYNTH_API_KEY"}
    try:
        r = requests.get(f"{config.SYNTH_BASE}/insights/polymarket/up-down/hourly",
                         headers={"Authorization": f"Apikey {config.SYNTH_API_KEY}"},
                         params={"asset": "BTC"}, timeout=10)
        if r.ok and "error" not in r.json():
            return {"connected": True, "message": "Synth SN50 connected"}
        return {"connected": False, "message": f"Error: {r.status_code}"}
    except Exception as e:
        return {"connected": False, "message": str(e)[:50]}
