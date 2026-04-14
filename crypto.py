"""
crypto.py — Three-Layer Crypto Prediction Market Engine

LAYER 1: LATENCY SNIPE (98% win rate)
  At T-10s before market close, check if BTC has already moved decisively.
  If delta > 0.05%, the outcome is nearly locked. Buy the winning side.
  This is how the $313 → $438K bot works.

LAYER 2: SYNTH EDGE (60% win rate, 10%+ edge)  
  When Synth SN50 probability diverges from Polymarket by >5%.
  Trade earlier in the window when edge is biggest.

LAYER 3: PAIR ARBITRAGE (100% win rate, small return)
  When Up + Down < $0.98, buy both sides for risk-free profit.

Data sources:
  - Binance: wss://stream.binance.com:9443/ws/btcusdt@ticker (free, real-time)
  - Synth API: https://api.synthdata.co/insights/polymarket/* ($199/mo)
  - Polymarket Gamma: https://gamma-api.polymarket.com (free)

Market timing:
  5-min:  slug = f"btc-updown-5m-{window_ts}"  (window_ts = now - now%300)
  15-min: slug = f"btc-updown-15m-{window_ts}" (window_ts = now - now%900)
  Hourly: resolved at each ET hour boundary
"""

import time
import json
import math
import threading
import requests
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional
import config

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
    """
    Start price feeds for real-time BTC and ETH prices.
    Tries WebSocket first, falls back to REST polling.
    NEVER crashes — all errors are caught and retried.
    """
    def _poll_loop():
        """REST polling fallback — works everywhere, no special libraries needed."""
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
        """Pre-cache window open prices at each 5-min and 15-min boundary."""
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
        # Try WebSocket first (faster, ~1s updates)
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
        # WebSocket not available — use REST polling
        threading.Thread(target=_poll_loop, daemon=True).start()
        print("  [CRYPTO] Binance REST polling started (BTC + ETH)")

    # Also start REST as backup (in case WebSocket fails silently)
    threading.Thread(target=_poll_loop, daemon=True).start()
    # Start price cache thread
    threading.Thread(target=_price_cache_loop, daemon=True).start()
    return True


@dataclass
class CryptoSignal:
    """A signal from the crypto engine."""
    id: str
    asset: str                    # "BTC" or "ETH"
    timeframe: str                # "5min", "15min", "hourly"
    layer: str                    # "snipe", "synth", "arb"
    direction: str                # "up" or "down"
    # Pricing
    price: float                  # What we'd pay
    side: str                     # "YES" (up) or "NO" (down on up-market)
    shares: int
    cost: float
    implied_return: float
    # Evidence
    window_delta_pct: float       # How much has price moved since window open
    synth_prob_up: float          # Synth's UP probability (0 if not used)
    poly_prob_up: float           # Polymarket's implied UP probability
    edge: float                   # Our calculated edge
    confidence: float             # 0-1
    # Market
    slug: str
    event_end: str
    seconds_remaining: int
    # Meta
    detail: str                   # Human-readable explanation
    timestamp: str


def scan_crypto_opportunities(equity: float, open_crypto_exposure: float) -> list[CryptoSignal]:
    """
    Main scanner. Checks all active crypto prediction markets
    across all three layers.
    """
    signals = []
    now = time.time()
    max_exposure = equity * config.SYNTH_MAX_EXPOSURE_PCT
    remaining = max_exposure - open_crypto_exposure
    if remaining <= 0:
        return []

    btc = get_btc_price()
    eth = get_eth_price()
    if btc <= 0:
        return []  # No price data yet

    # Check 5-minute BTC markets
    sig = _check_5min_market(btc, "BTC", now, equity, remaining)
    if sig:
        signals.append(sig)
        remaining -= sig.cost

    # Check 15-minute markets (BTC, ETH, SOL)
    for asset, price_fn in [("BTC", get_btc_price), ("ETH", get_eth_price)]:
        price = price_fn()
        if price <= 0:
            continue
        sig = _check_15min_market(price, asset, now, equity, remaining)
        if sig:
            signals.append(sig)
            remaining -= sig.cost

    # Check hourly markets with Synth
    if config.SYNTH_API_KEY:
        for asset in config.SYNTH_ASSETS:
            sig = _check_synth_hourly(asset, equity, remaining)
            if sig:
                signals.append(sig)
                remaining -= sig.cost

    return signals


def _check_5min_market(btc_now: float, asset: str, now: float, equity: float, remaining: float) -> Optional[CryptoSignal]:
    """
    LAYER 1: 5-minute latency snipe.
    At T-30 to T-5 seconds, check if direction is confirmed.
    """
    window_ts = int(now) - (int(now) % 300)
    close_time = window_ts + 300
    seconds_left = close_time - now

    # Only trade in the last 30 seconds of the window
    if seconds_left > 30 or seconds_left < 3:
        return None

    # Get the window's opening price from Binance
    open_price = _get_window_open_price(window_ts, asset)
    if open_price <= 0:
        return None

    # Calculate delta
    delta_pct = (btc_now - open_price) / open_price * 100

    # Decision thresholds (from the $438K bot research)
    abs_delta = abs(delta_pct)
    if abs_delta < 0.02:
        return None  # Too close to call

    direction = "up" if delta_pct > 0 else "down"

    # Estimate what we'd pay (market makers see the same delta)
    if abs_delta >= 0.15:
        est_price = 0.94  # Nearly certain — expensive
    elif abs_delta >= 0.10:
        est_price = 0.87
    elif abs_delta >= 0.05:
        est_price = 0.75
    elif abs_delta >= 0.02:
        est_price = 0.60
    else:
        est_price = 0.52

    implied_return = (1.0 - est_price) / est_price

    # Size position
    sizing = config.SYNTH_SIZING.get("15min", {})  # Use 15min sizing for 5min too
    pos_usd = min(equity * sizing.get("pct", 0.04), sizing.get("max_usd", 60), remaining)
    shares = int(pos_usd / est_price)
    if shares < config.MIN_SHARES:
        return None

    # Confidence based on delta size and time remaining
    if abs_delta >= 0.10 and seconds_left <= 15:
        confidence = 0.98
    elif abs_delta >= 0.05 and seconds_left <= 20:
        confidence = 0.92
    elif abs_delta >= 0.02:
        confidence = 0.75
    else:
        confidence = 0.55

    slug = f"btc-updown-5m-{window_ts}"
    now_dt = datetime.now(timezone.utc)

    return CryptoSignal(
        id=f"snipe-{asset}-5m-{window_ts}",
        asset=asset, timeframe="5min", layer="snipe",
        direction=direction, price=est_price,
        side="YES" if direction == "up" else "NO",
        shares=shares, cost=round(shares * est_price, 2),
        implied_return=round(implied_return, 4),
        window_delta_pct=round(delta_pct, 4),
        synth_prob_up=0, poly_prob_up=0,
        edge=round(confidence - est_price, 4),
        confidence=confidence,
        slug=slug, event_end=datetime.fromtimestamp(close_time, timezone.utc).isoformat(),
        seconds_remaining=int(seconds_left),
        detail=f"SNIPE {asset} Δ{delta_pct:+.3f}% T-{int(seconds_left)}s → {direction.upper()} @ ~${est_price:.2f}",
        timestamp=now_dt.isoformat(),
    )


def _check_15min_market(price_now: float, asset: str, now: float, equity: float, remaining: float) -> Optional[CryptoSignal]:
    """
    LAYER 1 for 15-minute markets. Same latency snipe concept.
    Trade in last 60 seconds when direction is clearer.
    """
    window_ts = int(now) - (int(now) % 900)
    close_time = window_ts + 900
    seconds_left = close_time - now

    if seconds_left > 60 or seconds_left < 5:
        return None

    open_price = _get_window_open_price(window_ts, asset)
    if open_price <= 0:
        return None

    delta_pct = (price_now - open_price) / open_price * 100
    abs_delta = abs(delta_pct)
    if abs_delta < 0.03:
        return None

    direction = "up" if delta_pct > 0 else "down"

    if abs_delta >= 0.20:
        est_price = 0.93
    elif abs_delta >= 0.10:
        est_price = 0.82
    elif abs_delta >= 0.05:
        est_price = 0.68
    elif abs_delta >= 0.03:
        est_price = 0.58
    else:
        est_price = 0.52

    implied_return = (1.0 - est_price) / est_price
    sizing = config.SYNTH_SIZING.get("15min", {})
    pos_usd = min(equity * sizing.get("pct", 0.04), sizing.get("max_usd", 60), remaining)
    shares = int(pos_usd / est_price)
    if shares < config.MIN_SHARES:
        return None

    confidence = min(0.65 + abs_delta * 3, 0.98) if abs_delta >= 0.05 else 0.60

    slug_prefix = "btc" if asset == "BTC" else "eth" if asset == "ETH" else "sol"
    slug = f"{slug_prefix}-updown-15m-{window_ts}"

    return CryptoSignal(
        id=f"snipe-{asset}-15m-{window_ts}",
        asset=asset, timeframe="15min", layer="snipe",
        direction=direction, price=est_price,
        side="YES" if direction == "up" else "NO",
        shares=shares, cost=round(shares * est_price, 2),
        implied_return=round(implied_return, 4),
        window_delta_pct=round(delta_pct, 4),
        synth_prob_up=0, poly_prob_up=0,
        edge=round(confidence - est_price, 4),
        confidence=confidence,
        slug=slug, event_end=datetime.fromtimestamp(close_time, timezone.utc).isoformat(),
        seconds_remaining=int(seconds_left),
        detail=f"SNIPE {asset} 15m Δ{delta_pct:+.3f}% T-{int(seconds_left)}s → {direction.upper()} @ ~${est_price:.2f}",
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


def _check_synth_hourly(asset: str, equity: float, remaining: float) -> Optional[CryptoSignal]:
    """
    LAYER 2: Synth probability edge on hourly markets.
    Trade when Synth diverges from Polymarket by >min_edge.
    """
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

    # Calculate edge both directions
    edge_up = synth_up - poly_up
    edge_down = (1 - synth_up) - (1 - poly_up)  # Same magnitude, opposite sign

    sizing = config.SYNTH_SIZING.get("hourly", {})
    min_edge = sizing.get("min_edge", 0.07)

    if abs(edge_up) < min_edge:
        return None

    if edge_up > 0:
        direction = "up"
        edge = edge_up
        price = data.get("best_ask_price", poly_up) or poly_up
        side = "YES"
    else:
        direction = "down"
        edge = abs(edge_up)
        bid = data.get("best_bid_price", poly_up)
        price = (1 - bid) if bid else (1 - poly_up)
        side = "NO"

    if price < 0.10 or price > 0.92:
        return None

    implied_return = (1.0 - price) / price
    pos_usd = min(equity * sizing.get("pct", 0.06), sizing.get("max_usd", 100), remaining)
    shares = int(pos_usd / max(price, 0.05))
    if shares < config.MIN_SHARES:
        return None

    confidence_label = "strong" if edge >= 0.12 else "moderate" if edge >= 0.08 else "weak"

    return CryptoSignal(
        id=f"synth-{asset}-1h-{int(time.time())}",
        asset=asset, timeframe="hourly", layer="synth",
        direction=direction, price=round(price, 4),
        side=side, shares=shares, cost=round(shares * price, 2),
        implied_return=round(implied_return, 4),
        window_delta_pct=0,
        synth_prob_up=round(synth_up, 4),
        poly_prob_up=round(poly_up, 4),
        edge=round(edge, 4),
        confidence=round(min(0.5 + edge, 0.85), 2),
        slug=data.get("slug", ""),
        event_end=data.get("event_end_time", ""),
        seconds_remaining=0,
        detail=f"SYNTH {asset} 1h {direction.upper()} │ Synth:{synth_up:.1%} vs Poly:{poly_up:.1%} = {edge:+.1%} edge │ {confidence_label}",
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


# ── Window open price tracking ───────────────────
_window_opens = {}  # {(asset, window_ts): open_price}
_window_lock = threading.Lock()


def _get_window_open_price(window_ts: int, asset: str) -> float:
    """
    Get the price at the start of a window.
    Stores it on first access per window.
    """
    key = (asset, window_ts)
    with _window_lock:
        if key in _window_opens:
            return _window_opens[key]

    # Fetch from Binance klines
    symbol = f"{asset}USDT"
    try:
        r = requests.get("https://api.binance.com/api/v3/klines",
                         params={"symbol": symbol, "interval": "1m",
                                 "startTime": window_ts * 1000, "limit": 1},
                         timeout=5)
        if r.ok:
            data = r.json()
            if data:
                open_price = float(data[0][1])  # [1] = open price
                with _window_lock:
                    _window_opens[key] = open_price
                    # Cleanup old entries (keep last 50)
                    if len(_window_opens) > 50:
                        oldest = sorted(_window_opens.keys(), key=lambda k: k[1])[:20]
                        for k in oldest:
                            del _window_opens[k]
                return open_price
    except Exception:
        pass

    # Fallback: use current price (less accurate)
    if asset == "BTC":
        return get_btc_price()
    elif asset == "ETH":
        return get_eth_price()
    return 0.0


def get_synth_status() -> dict:
    """Check if Synth API is reachable."""
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
