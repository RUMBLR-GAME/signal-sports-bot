"""
markets.py — Polymarket Sports Market Scanner

Scans Gamma API for active sports markets.
Matches against ESPN verified games.
Returns harvest opportunities.
"""

import json
import requests
from dataclasses import dataclass
import config
from espn import VerifiedGame, team_search_terms


@dataclass
class HarvestTarget:
    id: str
    sport: str
    event_title: str
    outcome: str
    side: str
    token_id: str
    condition_id: str
    price: float
    shares: int
    cost: float
    implied_return: float
    confidence: float
    level: str
    score_line: str
    leader_team: str
    lead: int
    elapsed_pct: float
    volume: float
    liquidity: float


def scan_for_harvests(verified_games: list[VerifiedGame], equity: float,
                       open_harvest_exposure: float) -> list[HarvestTarget]:
    if not verified_games or not config.HARVEST_ENABLED:
        return []

    max_harvest = equity * config.HARVEST_MAX_EXPOSURE_PCT
    if open_harvest_exposure >= max_harvest:
        return []

    poly_events = _fetch_poly_events()
    if not poly_events:
        return []

    targets = []
    for game in verified_games:
        if game.level == "final":
            continue  # Can't buy resolved markets
        matches = _match_game(game, poly_events)
        for m in matches:
            price = m["price"]
            if game.level in ("blowout", "strong", "safe"):
                min_price = config.HARVEST_VERIFIED_MIN
            else:
                min_price = config.HARVEST_UNVERIFIED_MIN

            if not (min_price <= price <= config.HARVEST_VERIFIED_MAX):
                continue

            ret = (1.0 - price) / price
            if ret < config.HARVEST_MIN_RETURN:
                continue

            vol = m["market"].get("volume", 0)
            if vol < config.HARVEST_MIN_VOLUME:
                continue

            pos_usd = min(equity * config.HARVEST_POSITION_PCT, config.HARVEST_MAX_USD)
            shares = int(pos_usd / price)
            if shares < config.MIN_SHARES:
                continue

            targets.append(HarvestTarget(
                id=f"h-{m['market']['condition_id'][:10]}-{game.espn_id}",
                sport=game.sport, event_title=m["event_title"],
                outcome=m["outcome_label"], side=m["side"],
                token_id=m["token_id"], condition_id=m["market"]["condition_id"],
                price=price, shares=shares, cost=round(shares * price, 2),
                implied_return=round(ret, 4), confidence=game.confidence,
                level=game.level, score_line=game.score_line,
                leader_team=game.leader_team, lead=game.lead,
                elapsed_pct=game.elapsed_pct, volume=vol,
                liquidity=m["market"].get("liquidity", 0),
            ))

    # Dedup by condition_id, sort by confidence
    seen = set()
    unique = []
    for t in sorted(targets, key=lambda x: (x.confidence, x.implied_return), reverse=True):
        if t.condition_id not in seen:
            seen.add(t.condition_id)
            unique.append(t)
    return unique[:25]


def _fetch_poly_events() -> list[dict]:
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
    matches = []
    leader_terms = team_search_terms(game.leader_team,
                                      game.home_abbrev if game.leader == "home" else game.away_abbrev)
    trailer_terms = team_search_terms(game.trailer_team,
                                       game.away_abbrev if game.leader == "home" else game.home_abbrev)

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

            try:
                op = mkt.get("outcomePrices", "")
                prices = json.loads(op) if isinstance(op, str) else []
                p_yes = float(prices[0]) if prices else 0
                p_no = float(prices[1]) if len(prices) > 1 else 1 - p_yes
            except (json.JSONDecodeError, ValueError, IndexError):
                continue

            try:
                ct = mkt.get("clobTokenIds", "")
                tokens = json.loads(ct) if isinstance(ct, str) else (ct if isinstance(ct, list) else [])
            except json.JSONDecodeError:
                continue
            tok_yes = tokens[0] if tokens else ""
            tok_no = tokens[1] if len(tokens) > 1 else ""

            outcomes = mkt.get("outcomes", "")
            if isinstance(outcomes, str):
                try: outcomes = json.loads(outcomes)
                except: outcomes = ["Yes", "No"]
            label = outcomes[0] if outcomes else ""
            label_lower = label.lower()

            cid = mkt.get("conditionId", mkt.get("id", ""))
            mkt_data = {"condition_id": cid, "volume": float(mkt.get("volume", 0)),
                        "liquidity": float(mkt.get("liquidity", 0))}

            if any(t in label_lower for t in leader_terms):
                if config.HARVEST_VERIFIED_MIN <= p_yes <= config.HARVEST_VERIFIED_MAX:
                    matches.append({"market": mkt_data, "price": p_yes, "token_id": tok_yes,
                                    "side": "YES", "outcome_label": label, "event_title": title})
            elif any(t in label_lower for t in trailer_terms):
                if config.HARVEST_VERIFIED_MIN <= p_no <= config.HARVEST_VERIFIED_MAX:
                    matches.append({"market": mkt_data, "price": p_no, "token_id": tok_no,
                                    "side": "NO", "outcome_label": f"NOT {label}", "event_title": title})
    return matches
