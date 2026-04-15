"""
clob.py — Polymarket CLOB Interface

All interactions with the Polymarket orderbook in one place.
Two modes:
  - Read-only (no auth): price queries, orderbook data
  - Authenticated (Level 2): order placement, fill tracking, balance

MAKER ORDERS ONLY. Zero fees + daily USDC rebates.
Never use taker/market orders (1.80% fee on crypto, 0.75% on sports).

Key gotchas handled:
  - post_only=True → reject if order would take liquidity
  - Never call update_balance_allowance() after fills
  - 500ms delay before balance check post-fill
  - Rate limiting at 55/min (CLOB limit is 60)
  - get_tick_size() before every order
"""

import time
import json
import logging
import threading
from typing import Optional
from dataclasses import dataclass, asdict

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    OrderArgs, OrderType, PartialCreateOrderOptions,
    OpenOrderParams, TradeParams, BalanceAllowanceParams,
)
from py_clob_client.order_builder.constants import BUY, SELL

import config

logger = logging.getLogger("clob")

# ── Rate limiting ────────────────────────────────
_order_times: list[float] = []
_rate_lock = threading.Lock()


def _rate_ok() -> bool:
    with _rate_lock:
        now = time.time()
        _order_times[:] = [t for t in _order_times if now - t < 60]
        if len(_order_times) >= config.RATE_LIMIT_PER_MIN:
            return False
        _order_times.append(now)
        return True


# ── Result types ─────────────────────────────────
@dataclass
class PriceData:
    token_id: str
    buy_price: float
    sell_price: float
    midpoint: float
    spread: float
    timestamp: float


@dataclass
class OrderResult:
    success: bool
    order_id: str = ""
    error: str = ""


# ── CLOB Client Singleton ────────────────────────
_read_client: Optional[ClobClient] = None
_auth_client: Optional[ClobClient] = None


def _get_reader() -> ClobClient:
    """Read-only CLOB client (no auth needed)."""
    global _read_client
    if _read_client is None:
        _read_client = ClobClient(config.CLOB_HOST)
    return _read_client


def _get_auth_client() -> Optional[ClobClient]:
    """Authenticated CLOB client for order placement."""
    global _auth_client
    if _auth_client is not None:
        return _auth_client
    if not config.PRIVATE_KEY:
        return None
    try:
        client = ClobClient(
            host=config.CLOB_HOST,
            chain_id=config.CHAIN_ID,
            key=config.PRIVATE_KEY,
            signature_type=config.SIGNATURE_TYPE,
            funder=config.FUNDER_ADDRESS or None,
        )
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        # Verify
        if client.get_ok():
            _auth_client = client
            logger.info("CLOB authenticated (Level 2)")
            return client
        else:
            logger.error("CLOB auth health check failed")
            return None
    except Exception as e:
        logger.error(f"CLOB auth failed: {e}")
        return None


def is_authenticated() -> bool:
    return _get_auth_client() is not None


# ══════════════════════════════════════════════════
# READ-ONLY OPERATIONS (no auth)
# ══════════════════════════════════════════════════

def get_price(token_id: str) -> Optional[PriceData]:
    """Get real orderbook price for a token. No auth needed."""
    if not token_id:
        return None
    try:
        reader = _get_reader()

        buy_r = reader.get_price(token_id, "BUY")
        buy = float(buy_r["price"]) if isinstance(buy_r, dict) else float(buy_r)

        sell_r = reader.get_price(token_id, "SELL")
        sell = float(sell_r["price"]) if isinstance(sell_r, dict) else float(sell_r)

        mid_r = reader.get_midpoint(token_id)
        mid = float(mid_r["mid"]) if isinstance(mid_r, dict) else float(mid_r)

        spr_r = reader.get_spread(token_id)
        spr = float(spr_r["spread"]) if isinstance(spr_r, dict) else float(spr_r)

        return PriceData(
            token_id=token_id,
            buy_price=buy, sell_price=sell,
            midpoint=mid, spread=spr,
            timestamp=time.time(),
        )
    except Exception as e:
        logger.error(f"Price query failed for {token_id[:16]}…: {e}")
        return None


def health_check() -> dict:
    """Check CLOB connectivity and auth status."""
    result = {"read": False, "auth": False, "error": ""}
    try:
        reader = _get_reader()
        reader.get_ok()
        result["read"] = True
    except Exception as e:
        result["error"] = f"Read: {e}"
    result["auth"] = is_authenticated()
    return result


# ══════════════════════════════════════════════════
# ORDER OPERATIONS (requires auth)
# ══════════════════════════════════════════════════

def place_limit_buy(token_id: str, price: float, size: float) -> OrderResult:
    """
    Place a MAKER limit BUY order.
    post_only=True ensures we never accidentally take liquidity.
    
    Returns OrderResult with order_id on success.
    """
    client = _get_auth_client()
    if not client:
        return OrderResult(success=False, error="Not authenticated")

    if not _rate_ok():
        return OrderResult(success=False, error="Rate limited")

    if price < 0.01 or price > 0.99:
        return OrderResult(success=False, error=f"Bad price: {price}")
    if size < config.MIN_SHARES:
        return OrderResult(success=False, error=f"Size {size} < min {config.MIN_SHARES}")

    try:
        tick_size = client.get_tick_size(token_id)
        options = PartialCreateOrderOptions(tick_size=tick_size)

        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=BUY,
        )

        signed = client.create_order(order_args, options)
        resp = client.post_order(signed, OrderType.GTC, post_only=True)

        if isinstance(resp, dict):
            oid = resp.get("orderID", resp.get("id", ""))
            err = resp.get("error", "")
            if err:
                return OrderResult(success=False, error=err)
            if oid:
                logger.info(f"LIMIT BUY posted: {size}× @ ${price:.4f} → {oid[:16]}")
                return OrderResult(success=True, order_id=oid)
            return OrderResult(success=False, error="No order ID in response")
        else:
            return OrderResult(success=True, order_id=str(resp))

    except Exception as e:
        logger.error(f"Order error: {e}")
        return OrderResult(success=False, error=str(e)[:200])


def cancel_order(order_id: str) -> bool:
    """Cancel an open order. Returns True on success."""
    client = _get_auth_client()
    if not client:
        return False
    try:
        client.cancel(order_id)
        logger.info(f"Cancelled: {order_id[:16]}")
        return True
    except Exception as e:
        logger.error(f"Cancel failed: {e}")
        return False


def cancel_all_orders() -> bool:
    """Cancel all open orders."""
    client = _get_auth_client()
    if not client:
        return False
    try:
        client.cancel_all()
        logger.info("Cancelled all open orders")
        return True
    except Exception as e:
        logger.error(f"Cancel all failed: {e}")
        return False


def check_order_filled(order_id: str) -> Optional[dict]:
    """
    Check if an order has been filled.
    Returns {'filled': bool, 'size_matched': float, 'price': float} or None.
    
    IMPORTANT: Fill notifications can be lost on connection drops.
    Always poll this rather than relying on events.
    """
    client = _get_auth_client()
    if not client:
        return None
    try:
        orders = client.get_orders(OpenOrderParams(id=order_id))
        if isinstance(orders, list) and len(orders) > 0:
            o = orders[0]
            matched = float(o.get("size_matched", 0))
            original = float(o.get("original_size", 0))
            return {
                "filled": matched >= original * 0.95 if original > 0 else False,
                "size_matched": matched,
                "price": float(o.get("price", 0)),
                "status": o.get("status", "unknown"),
            }
        elif isinstance(orders, list) and len(orders) == 0:
            # Order not in open orders — might be filled or expired
            # Wait 500ms per research before checking trades
            time.sleep(config.POST_FILL_DELAY)
            return {"filled": True, "size_matched": 0, "price": 0, "status": "gone"}
        return None
    except Exception as e:
        logger.error(f"Fill check error: {e}")
        return None


def get_balance() -> dict:
    """
    Get USDC balance. NEVER call update_balance_allowance() — it overwrites state.
    """
    client = _get_auth_client()
    if not client:
        return {"balance": 0, "allowance": 0}
    try:
        r = client.get_balance_allowance(
            BalanceAllowanceParams(signature_type=config.SIGNATURE_TYPE)
        )
        if isinstance(r, dict):
            return {
                "balance": float(r.get("balance", 0)),
                "allowance": float(r.get("allowance", 0)),
            }
        return {"balance": 0, "allowance": 0}
    except Exception as e:
        logger.error(f"Balance error: {e}")
        return {"balance": 0, "allowance": 0}
