"""
scanner.py — Harvest Signal Scanner

ESPN verified blowouts → Polymarket market match → real CLOB price → EV calculation → Kelly sizing.

This module is PURE signal generation. It never places orders.
It returns a list of HarvestSignal objects ready for execution.
"""

import json
import logging
import requests
from dataclasses import dataclass
from typing import Optional

import config
from espn import VerifiedGame, fetch_verified_games, team_search_terms
from clob import get_price, PriceData

logger = logging.getLogger("scanner")


@dataclass
class HarvestSignal:
    """A fully-qualified harvest opportunity ready for execution."""
    id: str
    sport: str
    event_title: str
    # What we're betting on
    outcome: str               # "Boston Celtics" or "NOT Philadelphia 76ers"
    side: str                  # "YES" or "NO"
    leader_team: str
    # Market identifiers
    token_id: str              # CLOB token to buy
    condition_id: str          # Polymarket condition ID
    # Pricing (real CLOB data)
    price: float               # Real orderbook buy price
    spread: float              # Bid-ask spread
    price_source: str          # "clob" or "gamma"
    # Sizing (Kelly)
    shares: int
    cost: float
    implied_return: float      # (1 - price) / price
    ev_per_share: float        # confidence - price
    # Game context
    confidence: float          # ESPN-derived win probability
    level: str                 # "blowout", "strong", "safe"
    score_line: str            # "BOS 112 - PHI 84 4th 3:22"
    lead: int
    elapsed_pct: float
    volume: float
    liquidity: float


def scan(equity: float, current_exposure: float) -> tuple[list, list]:
    """
    Full scan cycle:
    1. Fetch ESPN verified blowouts
    2. Find matching Polymarket markets
    3. Query real CLOB prices
    4. Calculate EV and Kelly size
    5. Return (signals, games) tuple — always a tuple, never bare list
    """
    # Check capacity
    max_exposure = equity * config.MAX_EXPOSURE_PCT
    remaining = max_exposure - current_exposure
    if remaining <= 0:
        return [], []

    # Step 1: ESPN
    games = fetch_verified_games()
    live_games = [g for g in games if g.level != "final"]
    if not live_games:
        return [], games

    # Step 2: Polymarket events
    poly_events = _fetch_poly_events()
    if not poly_events:
        return [], games

    # Step 3+4+5: Match, price, size
    signals = []
    for game in live_games:
        matches = _match_game(game, poly_events)
        for m in matches:
            sig = _build_signal(game, m, equity, remaining)
            if sig:
                signals.append(sig)
                remaining -= sig.cost
                if remaining <= 0:
                    break
        if remaining <= 0:
            break

    # Sort by EV (best opportunities first)
    signals.sort(key=lambda s: s.ev_per_share, reverse=True)

    return signals, games


def _build_signal(game: VerifiedGame, match: dict, equity: float, remaining: float) -> Optional[HarvestSignal]:
    """Build a fully-qualified signal from a game + market match."""
    gamma_price = match["price"]
    token_id = match["token_id"]

    # ── Real CLOB price ──
    price = gamma_price
    spread = 0.0
    source = "gamma"

    if token_id:
        clob_data = get_price(token_id)
        if clob_data:
            price = clob_data.buy_price
            spread = clob_data.spread
            source = "clob"

            # Skip illiquid markets
            if spread > config.MAX_SPREAD:
                logger.debug(f"Skip {match['outcome_label']}: spread {spread:.3f} > {config.MAX_SPREAD}")
                return None

    # ── Price gates ──
    if not (config.PRICE_MIN <= price <= config.PRICE_MAX):
        return None

    ret = (1.0 - price) / price
    if ret < config.MIN_RETURN:
        return None

    # ── Volume gate ──
    vol = match["market"].get("volume", 0)
    if vol < config.MIN_VOLUME:
        return None

    # ── EV calculation ──
    ev = game.confidence * 1.0 - price
    if ev < config.MIN_EV:
        logger.debug(f"Skip {match['outcome_label']}: EV {ev:.4f} < {config.MIN_EV}")
        return None

    # ── Kelly sizing ──
    shares = _kelly_size(game.confidence, price, equity, remaining)
    if shares < config.MIN_SHARES:
        return None

    cost = round(shares * price, 2)

    return HarvestSignal(
        id=f"h-{match['market']['condition_id'][:10]}-{game.espn_id}",
        sport=game.sport, event_title=match["event_title"],
        outcome=match["outcome_label"], side=match["side"],
        leader_team=game.leader_team,
        token_id=token_id, condition_id=match["market"]["condition_id"],
        price=round(price, 4), spread=round(spread, 4), price_source=source,
        shares=shares, cost=cost, implied_return=round(ret, 4),
        ev_per_share=round(ev, 4),
        confidence=game.confidence, level=game.level,
        score_line=game.score_line, lead=game.lead,
        elapsed_pct=game.elapsed_pct,
        volume=vol, liquidity=match["market"].get("liquidity", 0),
    )


def _kelly_size(win_prob: float, price: float, equity: float, remaining: float) -> int:
    """
    Kelly criterion position sizing for binary markets.
    
    Payout: $1.00 if win, $0.00 if loss
    Kelly f* = (p × b - q) / b  where b = (1-price)/price, q = 1-p
    We use quarter-Kelly with hard caps.
    
    Maker orders = zero fees, so no fee adjustment needed.
    """
    if price <= 0 or price >= 1 or win_prob <= price:
        return 0

    b = (1.0 - price) / price  # payout ratio
    q = 1.0 - win_prob

    kelly = (win_prob * b - q) / b
    kelly = max(kelly, 0.0)

    # Quarter-Kelly with caps
    fraction = kelly * config.KELLY_FRACTION
    fraction = min(fraction, config.KELLY_MAX_PCT, config.MAX_SINGLE_BET_PCT)

    pos_usd = min(equity * fraction, config.MAX_SINGLE_BET_USD, remaining)
    shares = int(pos_usd / price)

    return shares if shares >= config.MIN_SHARES else 0


def _fetch_poly_events() -> list[dict]:
    """Fetch active sports events from Polymarket Gamma API."""
    all_events = []
    seen = set()
    tags = set(["sports"])
    for cfg in config.ESPN_SPORTS.values():
        tags.update(cfg["poly_tags"])

    for tag in tags:
        try:
            r = requests.get(f"{config.GAMMA_API}/events",
                             params={"active": "true", "tag_slug": tag, "limit": 50,
                                     "order": "volume_24hr", "ascending": "false"}, timeout=12)
            if not r.ok:
                continue
            for ev in r.json():
                eid = ev.get("id", "")
                if eid and eid not in seen:
                    seen.add(eid)
                    all_events.append(ev)
        except Exception:
            continue
    return all_events


def _match_game(game: VerifiedGame, poly_events: list[dict]) -> list[dict]:
    """Match an ESPN verified game to Polymarket markets."""
    matches = []
    leader_terms = team_search_terms(
        game.leader_team,
        game.home_abbrev if game.leader == "home" else game.away_abbrev,
    )
    trailer_terms = team_search_terms(
        game.trailer_team,
        game.away_abbrev if game.leader == "home" else game.home_abbrev,
    )

    for ev in poly_events:
        title = ev.get("title", "")
        tl = title.lower()
        if not any(t in tl for t in leader_terms) and not any(t in tl for t in trailer_terms):
            continue
        if any(kw in tl for kw in config.FUTURES_BLOCK):
            continue

        for mkt in ev.get("markets", []):
            q = (mkt.get("question", "") or mkt.get("groupItemTitle", "")).lower()
            if any(kw in q for kw in config.FUTURES_BLOCK):
                continue

            # Parse prices
            try:
                op = mkt.get("outcomePrices", "")
                prices = json.loads(op) if isinstance(op, str) else []
                p_yes = float(prices[0]) if prices else 0
                p_no = float(prices[1]) if len(prices) > 1 else 1 - p_yes
            except (json.JSONDecodeError, ValueError, IndexError):
                continue

            # Parse token IDs
            try:
                ct = mkt.get("clobTokenIds", "")
                tokens = json.loads(ct) if isinstance(ct, str) else (ct if isinstance(ct, list) else [])
            except json.JSONDecodeError:
                continue
            tok_yes = tokens[0] if tokens else ""
            tok_no = tokens[1] if len(tokens) > 1 else ""

            # Parse outcomes
            outcomes = mkt.get("outcomes", "")
            if isinstance(outcomes, str):
                try:
                    outcomes = json.loads(outcomes)
                except Exception:
                    outcomes = ["Yes", "No"]
            label = outcomes[0] if outcomes else ""
            label_lower = label.lower()

            cid = mkt.get("conditionId", mkt.get("id", ""))
            mkt_data = {
                "condition_id": cid,
                "volume": float(mkt.get("volume", 0)),
                "liquidity": float(mkt.get("liquidity", 0)),
            }

            # Match leader → YES side
            if any(t in label_lower for t in leader_terms):
                if config.PRICE_MIN <= p_yes <= config.PRICE_MAX:
                    matches.append({
                        "market": mkt_data, "price": p_yes, "token_id": tok_yes,
                        "side": "YES", "outcome_label": label, "event_title": title,
                    })
            # Match trailer → NO side (bet against them)
            elif any(t in label_lower for t in trailer_terms):
                if config.PRICE_MIN <= p_no <= config.PRICE_MAX:
                    matches.append({
                        "market": mkt_data, "price": p_no, "token_id": tok_no,
                        "side": "NO", "outcome_label": f"NOT {label}", "event_title": title,
                    })

    return matches
