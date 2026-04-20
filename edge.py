"""
edge.py — Engine 2: Edge Finder / Convergence Trade (v18)

Changes from v17:
  • Accepts combined pregame odds list (ESPN for US + Odds API for soccer)
  • Uses match_game_to_market (which applies ambiguity rule)
  • New sizing signature (returns reason, used for diagnostics)
  • Stale-quote penalty on edge (reduces effective edge, not hard block)
  • Exit path uses new positions.exit_position semantics
"""
import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, List, Tuple

from espn import GameOdds
from teams import match_game_to_market, find_team_in_outcomes
from clob import ClobInterface, parse_market_tokens
from positions import PositionManager
from sizing import compute_bet_size
from config import (
    EDGE_MIN_EDGE, EDGE_MAX_PRICE, EDGE_MIN_PRICE,
    EDGE_MIN_HOURS_BEFORE, EDGE_MAX_HOURS_BEFORE,
    EDGE_EXIT_REMAINING, EDGE_STOP_LOSS,
    EDGE_PRE_GAME_EXIT_MIN, EDGE_STALE_HOURS,
    EDGE_REENTRY_ENABLED, EDGE_REENTRY_COOLDOWN_MIN,
    EDGE_SHARPNESS_PREMIUM, EDGE_SHARPNESS_MIN_AGREE,
    MIN_MARKET_LIQUIDITY, MIN_DAILY_VOLUME,
    POLY_STALE_QUOTE_SEC, POLY_STALE_PENALTY,
    MAX_TOTAL_EXPOSURE_PCT, MAX_OPEN_POSITIONS, STARTING_BANKROLL,
)
from harvest import _market_is_stale

logger = logging.getLogger("edge")


@dataclass
class EdgeSignal:
    engine: str = "edge"
    sport: str = ""
    espn_id: str = ""
    condition_id: str = ""
    market_question: str = ""
    team: str = ""
    bet_outcome: str = ""
    outcome_idx: int = 0
    token_id: str = ""
    clob_price: float = 0.0
    true_prob: float = 0.0
    edge: float = 0.0
    provider: str = ""
    moneyline: int = 0
    bet_size: float = 0.0
    confidence: float = 0.0
    commence_time: str = ""
    sizing_reason: str = ""


def _hours_until(commence_time: str) -> Optional[float]:
    if not commence_time:
        return None
    try:
        ct = commence_time.replace("Z", "+00:00")
        game_dt = datetime.fromisoformat(ct)
        if game_dt.tzinfo is None:
            game_dt = game_dt.replace(tzinfo=timezone.utc)
        return (game_dt - datetime.now(timezone.utc)).total_seconds() / 3600.0
    except Exception:
        return None


def _ts_until(commence_time: str) -> Optional[float]:
    if not commence_time:
        return None
    try:
        ct = commence_time.replace("Z", "+00:00")
        return datetime.fromisoformat(ct).timestamp()
    except Exception:
        return None


async def scan_edge(
    clob: ClobInterface, positions: PositionManager,
    all_odds: List[GameOdds],
    lineup_watcher=None,
) -> Tuple[List[EdgeSignal], List[dict]]:
    """
    all_odds: combined list of GameOdds from ESPN + Odds API.
    lineup_watcher: optional LineupWatcher instance. When provided, its
      signals shift effective edge for matches where team news has landed.
    Returns (signals, all_edges_for_dashboard).
    """
    signals: List[EdgeSignal] = []
    all_edges: List[dict] = []

    if not all_odds:
        return signals, all_edges, {"sports_with_odds": 0, "total_odds": 0}

    # Diagnostic counters for dashboard
    diag = {
        "sports_with_odds": 0,
        "total_odds": len(all_odds),
        "skipped_no_polymarket_events": 0,
        "skipped_exposure_cap": 0,
        "skipped_position_cap": 0,
        "skipped_duplicate_game": 0,
        "skipped_wrong_time_window": 0,
        "skipped_no_market_match": 0,
        "skipped_low_liquidity": 0,
        "skipped_duplicate_condition": 0,
        "skipped_same_game_position": 0,
        "sides_evaluated": 0,
        "signals_generated": 0,
    }

    # Group by sport. Within each sport, further group by game so we can
    # compare multiple books' lines on the same match (sharpness signal).
    by_sport: dict = {}
    for o in all_odds:
        by_sport.setdefault(o.sport, []).append(o)
    diag["sports_with_odds"] = len(by_sport)

    for sport, odds_list in by_sport.items():
        # Fetch Polymarket events for this sport (once)
        events = await clob.fetch_polymarket_events(sport)
        if not events:
            diag["skipped_no_polymarket_events"] += len(odds_list)
            continue

        # Group by (home, away, date) — all book quotes for one game
        preferred = ["Pinnacle", "Betfair", "Bet365", "ESPN BET", "FanDuel", "BetMGM"]
        games: dict = {}  # game_key -> list of GameOdds
        for o in odds_list:
            k = (o.home_team.lower(), o.away_team.lower(), o.commence_time[:10])
            games.setdefault(k, []).append(o)
        # For each game, sort quotes by sharpness
        for k in games:
            games[k].sort(key=lambda o: preferred.index(o.provider) if o.provider in preferred else 99)

        for game_key, all_books in games.items():
            # Use the SHARPEST book as the primary truth (Pinnacle > Bet365 > others)
            odds = all_books[0]

            # Portfolio-wide caps: don't add new positions when we're at exposure limit.
            open_cost = sum(
                p.cost for p in positions.positions.values()
                if p.status in ("open", "filled")
            )
            projected_cost = open_cost + sum(s.bet_size for s in signals)
            exposure_pct = projected_cost / max(STARTING_BANKROLL, 1)
            if exposure_pct >= MAX_TOTAL_EXPOSURE_PCT:
                diag["skipped_exposure_cap"] += 1
                logger.debug(
                    f"EDGE: skipping — projected exposure {exposure_pct:.0%} >= cap {MAX_TOTAL_EXPOSURE_PCT:.0%}"
                )
                diag["_final_last"] = "exposure_cap_hit"
                return signals, all_edges, diag
            total_positions = len(positions.positions) + len(signals)
            if total_positions >= MAX_OPEN_POSITIONS:
                diag["skipped_position_cap"] += 1
                diag["_final_last"] = "position_cap_hit"
                return signals, all_edges, diag

            hours = _hours_until(odds.commence_time)
            if hours is None or hours < EDGE_MIN_HOURS_BEFORE or hours > EDGE_MAX_HOURS_BEFORE:
                diag["skipped_wrong_time_window"] += 1
                continue

            # Find Polymarket market
            parsed = None
            low_liq = False
            for ev in events:
                p = parse_market_tokens(ev)
                if not p:
                    continue
                if p.get("liquidity", 0) < MIN_MARKET_LIQUIDITY:
                    low_liq = True
                    continue
                if p.get("volume", 0) < MIN_DAILY_VOLUME:
                    low_liq = True
                    continue
                hi, ai = match_game_to_market(
                    odds.home_team, odds.home_abbrev,
                    odds.away_team, odds.away_abbrev,
                    p["question"], p["outcomes"],
                )
                if hi < 0 or ai < 0:
                    continue
                p["_home_idx"] = hi
                p["_away_idx"] = ai
                parsed = p
                break

            if not parsed:
                if low_liq:
                    diag["skipped_low_liquidity"] += 1
                else:
                    diag["skipped_no_market_match"] += 1
                continue

            # Check all condition_ids for this event (soccer has multiple —
            # home-win, away-win, draw are separate markets, must not trade both).
            cids_to_check = parsed.get("_condition_ids") or [parsed["condition_id"]]
            if any(positions.has_position_for(c) for c in cids_to_check):
                diag["skipped_duplicate_condition"] += 1
                continue

            # Re-entry logic: allow up to 2 entries per game, but only after
            # EDGE_REENTRY_COOLDOWN_MIN has passed since the last exit.
            # Avoids buying-selling-buying on noise.
            total_entries = positions.entries_for_game(
                odds.home_team, odds.away_team, engine="edge"
            )
            max_entries = 2 if EDGE_REENTRY_ENABLED else 1
            if total_entries >= max_entries:
                diag["skipped_same_game_position"] += 1
                continue
            # If we already have an OPEN position, don't open another
            if positions.has_position_for_game(
                odds.home_team, odds.away_team, engine="edge"
            ):
                diag["skipped_same_game_position"] += 1
                continue
            # If this is a re-entry, enforce cooldown since last exit
            if total_entries > 0:
                last_exit = positions.last_exit_time_for_game(
                    odds.home_team, odds.away_team, engine="edge"
                )
                if last_exit and (time.time() - last_exit) < EDGE_REENTRY_COOLDOWN_MIN * 60:
                    diag["skipped_reentry_cooldown"] = diag.get("skipped_reentry_cooldown", 0) + 1
                    continue

            hi = parsed["_home_idx"]
            ai = parsed["_away_idx"]
            stale = _market_is_stale(parsed)

            # ── Multi-book sharpness signal ──
            # If 2+ sharp books (Pinnacle/Bet365) agree on the price within
            # EDGE_SHARPNESS_MIN_AGREE, that's a strong signal our edge is real
            # (not one book being off). Apply EDGE_SHARPNESS_PREMIUM multiplier.
            sharpness_mult = 1.0
            agreeing_books = [odds.provider]
            for alt in all_books[1:]:
                if alt.provider in ("Pinnacle", "Bet365"):
                    if abs(alt.home_prob - odds.home_prob) <= EDGE_SHARPNESS_MIN_AGREE:
                        agreeing_books.append(alt.provider)
            if len(agreeing_books) >= 2 and "Pinnacle" in agreeing_books:
                sharpness_mult = EDGE_SHARPNESS_PREMIUM

            # Consult lineup watcher (if enabled) for team-news edge shift
            lineup_sig = None
            lineup_shift_home = 0.0
            lineup_shift_away = 0.0
            if lineup_watcher is not None:
                lineup_sig = lineup_watcher.get_signal(odds.home_team, odds.away_team)
                if lineup_sig:
                    # home_impact negative means home weakened → home true_prob ↓, away ↑
                    h_imp = lineup_sig.get("home_impact", 0.0)
                    a_imp = lineup_sig.get("away_impact", 0.0)
                    conf = lineup_sig.get("confidence", 0.0)
                    # Scale by confidence
                    lineup_shift_home = h_imp * conf
                    lineup_shift_away = a_imp * conf

            # Evaluate both sides BUT only trade the one with larger edge.
            # Taking both sides of the same match guarantees a loss on draw
            # and costs the spread on either result. One bet per game max.
            sides = [
                (odds.home_team, odds.home_prob + lineup_shift_home, odds.home_ml, hi, lineup_shift_home),
                (odds.away_team, odds.away_prob + lineup_shift_away, odds.away_ml, ai, lineup_shift_away),
            ]
            # Fetch prices + compute edges for both first
            side_edges = []
            for team, true_prob, ml, idx, lineup_adj in sides:
                true_prob = max(0.02, min(0.98, true_prob))
                tid = parsed["token_ids"][idx]
                poly_price = await clob.get_price(tid, "BUY")
                if poly_price is None:
                    try:
                        poly_price = float(parsed["prices"][idx])
                    except Exception:
                        poly_price = None
                if poly_price is None:
                    continue
                raw_edge = true_prob - poly_price
                eff_edge = raw_edge * POLY_STALE_PENALTY if stale else raw_edge
                side_edges.append({
                    "team": team, "true_prob": true_prob, "ml": ml, "idx": idx,
                    "tid": tid, "poly_price": poly_price,
                    "raw_edge": raw_edge, "eff_edge": eff_edge,
                    "lineup_adj": lineup_adj,
                })

            # Log ALL edges for diagnostics
            for se in side_edges:
                # Tag each side with its eventual disposition
                if se["eff_edge"] < EDGE_MIN_EDGE:
                    status = "SKIP_LOW_EDGE"
                    reason = f"edge {se['eff_edge']:.3f} < min {EDGE_MIN_EDGE:.2f}"
                elif se["poly_price"] < EDGE_MIN_PRICE:
                    status = "SKIP_PRICE_LOW"
                    reason = f"price {se['poly_price']:.3f} < min {EDGE_MIN_PRICE:.2f}"
                elif se["poly_price"] > EDGE_MAX_PRICE:
                    status = "SKIP_PRICE_HIGH"
                    reason = f"price {se['poly_price']:.3f} > max {EDGE_MAX_PRICE:.2f}"
                else:
                    status = "CANDIDATE"   # passed filters; may still lose to other side
                    reason = ""
                all_edges.append({
                    "team": se["team"], "sport": sport,
                    "poly": se["poly_price"], "true": se["true_prob"],
                    "edge": se["raw_edge"], "effective_edge": se["eff_edge"],
                    "provider": odds.provider, "moneyline": se["ml"],
                    "hours": round(hours, 1),
                    "stale": stale,
                    "lineup_adj": round(se["lineup_adj"], 4) if se["lineup_adj"] else 0,
                    "lineup_detail": (lineup_sig or {}).get("detail", "") if se["lineup_adj"] else "",
                    "commence_time": odds.commence_time,
                    "liquidity": parsed.get("liquidity", 0),
                    "status": status,
                    "reason": reason,
                })

            # Pick only the side with LARGEST effective edge
            tradable = [se for se in side_edges if se["eff_edge"] >= EDGE_MIN_EDGE
                        and EDGE_MIN_PRICE <= se["poly_price"] <= EDGE_MAX_PRICE]
            if not tradable:
                continue
            best = max(tradable, key=lambda x: x["eff_edge"])

            # Mark the non-best side as SKIP_NOT_BEST if both passed
            for e in all_edges:
                if (e.get("team") in (s["team"] for s in side_edges)
                    and e.get("status") == "CANDIDATE"
                    and e.get("team") != best["team"]
                    and e.get("commence_time") == odds.commence_time):
                    e["status"] = "SKIP_NOT_BEST_SIDE"
                    e["reason"] = f"other side has higher edge ({best['eff_edge']:.3f})"

            # Size it
            team = best["team"]
            true_prob = best["true_prob"]
            ml = best["ml"]
            idx = best["idx"]
            tid = best["tid"]
            poly_price = best["poly_price"]
            raw_edge = best["raw_edge"]
            eff_edge = best["eff_edge"]
            lineup_adj = best["lineup_adj"]

            start_ts = _ts_until(odds.commence_time)
            equity = positions.equity
            size, sz_reason = compute_bet_size(
                "edge", poly_price, true_prob, equity, positions,
                sport=sport, edge=eff_edge, game_start_ts=start_ts,
                pending_signals=signals,
            )
            if size <= 0:
                # Update the matching finding with SKIP_SIZE
                for e in all_edges:
                    if (e.get("team") == best["team"]
                        and e.get("commence_time") == odds.commence_time
                        and e.get("status") == "CANDIDATE"):
                        e["status"] = "SKIP_SIZE"
                        e["reason"] = sz_reason
                continue

            # Sharpness premium: multi-book agreement boosts size
            if sharpness_mult > 1.0:
                pre_premium = size
                size = round(size * sharpness_mult, 2)
                sz_reason += f" × sharpness {sharpness_mult:.2f} ({'+'.join(agreeing_books)})"
                # Re-apply caps in case sharpness pushed us over
                max_total = equity * 0.80  # MAX_TOTAL_EXPOSURE
                current_deployed = positions.open_cost + sum(s.bet_size for s in signals)
                size = min(size, max(0, max_total - current_deployed))

            # Mark best side as TRADED
            for e in all_edges:
                if (e.get("team") == best["team"]
                    and e.get("commence_time") == odds.commence_time
                    and e.get("status") == "CANDIDATE"):
                    e["status"] = "TRADED"
                    e["reason"] = f"bet ${size:.2f}" + (f" sharp×{sharpness_mult:.1f}" if sharpness_mult > 1 else "")

            signals.append(EdgeSignal(
                sport=sport, espn_id=odds.espn_id,
                    condition_id=parsed["condition_id"],
                    market_question=parsed["question"],
                    team=team,
                    bet_outcome=parsed["outcomes"][idx],
                    outcome_idx=idx,
                    token_id=tid, clob_price=poly_price,
                    true_prob=true_prob, edge=raw_edge,
                    provider=odds.provider, moneyline=ml,
                    bet_size=size, confidence=true_prob,
                    commence_time=odds.commence_time,
                    sizing_reason=sz_reason,
            ))
            logger.info(
                f"EDGE [{sport}]: {team} @{poly_price:.3f} true={true_prob:.3f} "
                f"edge={raw_edge:.3f}{' (stale→'+format(eff_edge,'.3f')+')' if stale else ''} "
                f"[{odds.provider}] ${size:.2f} | T-{hours:.1f}h"
            )
            diag["signals_generated"] += 1

    all_edges.sort(key=lambda e: e["edge"], reverse=True)
    diag["sides_evaluated"] = len(all_edges)
    return signals, all_edges[:300], diag


async def check_edge_exits(clob: ClobInterface, positions: PositionManager, latest_odds: Optional[dict] = None):
    """
    Priority:
      1. Pre-game exit (T-30min) — lock in the edge before game risk.
      2. Take profit: price moved ≥ EDGE_TAKE_PROFIT_PCT of the way from entry to true_prob.
      3. Stop loss (EDGE_STOP_LOSS below entry).
      4. Stale (held > EDGE_STALE_HOURS).

    latest_odds: optional {(home_lower, away_lower, commence_date): {provider: GameOdds}}
        cache of current bookmaker quotes. Used to snapshot Closing Line Value
        at pre-game exit (measures true alpha).

    In paper mode, clob.get_price returns None → fall back to HTTP midpoint.
    """
    from config import EDGE_TAKE_PROFIT_PCT
    for pos in positions.get_filled_by_engine("edge"):
        # Try authenticated SELL price first (real fills at bid)
        current = await clob.get_price(pos.token_id, "SELL")
        # Paper-mode fallback: HTTP midpoint
        if current is None:
            current = await clob.get_midpoint_http(pos.token_id)
        # Gamma fallback (stale but better than nothing)
        if current is None:
            try:
                current = float(pos.current_price or pos.entry_price)
            except Exception:
                current = None
        if current is None:
            continue
        positions.mark_current_price(pos.id, current)

        entry = pos.fill_price or pos.entry_price
        age_hours = (time.time() - pos.opened_at) / 3600.0
        age_min = age_hours * 60

        # 1. Pre-game exit — LOCK IN THE EDGE before game starts
        mins_to_game = None
        if pos.game_start_time:
            start_ts = _ts_until(pos.game_start_time)
            if start_ts is not None:
                mins_to_game = (start_ts - time.time()) / 60.0
        if mins_to_game is not None and mins_to_game <= EDGE_PRE_GAME_EXIT_MIN:
            # Snapshot CLV: lookup current book line for this game from latest_odds
            if latest_odds and pos.provider:
                try:
                    date_key = pos.game_start_time[:10] if pos.game_start_time else ""
                    for key, book_dict in latest_odds.items():
                        home_lc, away_lc, d = key
                        if d != date_key:
                            continue
                        if pos.team.lower() not in f"{home_lc} {away_lc}":
                            continue
                        provider_quote = book_dict.get(pos.provider)
                        if provider_quote:
                            if pos.outcome_idx == 0:
                                pos.clv_prob = provider_quote.home_prob
                            else:
                                pos.clv_prob = provider_quote.away_prob
                            pos.clv_snapshot_at = time.time()
                        break
                except Exception as e:
                    logger.debug(f"CLV snapshot failed for {pos.team}: {e}")
            await _do_exit(clob, positions, pos, current, f"pre_game (T-{mins_to_game:.0f}m)")
            continue

        # 1b. EXTREME PRICE — market has effectively resolved, exit NOW
        # If current price is >= 0.96, the market is pricing this as certain.
        # Waiting longer adds zero EV and just ties up capital.
        if current >= 0.96 and current > entry:
            await _do_exit(clob, positions, pos, current,
                           f"extreme_price (converged @{current:.3f})")
            continue
        # If current <= 0.04, the other side won. Exit for scrap.
        if current <= 0.04:
            await _do_exit(clob, positions, pos, current,
                           f"extreme_loss (resolved @{current:.3f})")
            continue

        # 2. Take profit: captured ≥ N% of max possible edge
        # Max edge = true_prob - entry (the full upside at position open).
        # We take profit when realized gain ≥ EDGE_TAKE_PROFIT_PCT of that.
        # 3-min hold prevents WS noise exits immediately after open.
        max_edge = max(0.0, pos.true_prob - entry)
        realized_edge = current - entry
        if max_edge > 0 and age_min >= 3:
            capture_pct = realized_edge / max_edge
            if capture_pct >= EDGE_TAKE_PROFIT_PCT:
                await _do_exit(
                    clob, positions, pos, current,
                    f"take_profit ({capture_pct:.0%} of max edge, {realized_edge*100:+.1f}¢)"
                )
                continue
        # Fallback take-profit when true_prob wasn't set (legacy positions):
        # exit if we've gained 5¢ or more (substantial convergence).
        elif realized_edge >= 0.05 and age_min >= 3:
            await _do_exit(
                clob, positions, pos, current,
                f"take_profit_fallback (+{realized_edge*100:.1f}¢)"
            )
            continue

        # 3. Stop loss
        if current < entry - EDGE_STOP_LOSS:
            await _do_exit(clob, positions, pos, current, f"stop_loss @{current:.3f}")
            continue

        # 4. Stale
        if age_hours > EDGE_STALE_HOURS:
            await _do_exit(clob, positions, pos, current, f"stale (held {age_hours:.1f}h)")
            continue


async def _do_exit(clob, positions, pos, price, reason):
    if clob.is_authenticated():
        ok = await clob.place_order(pos.token_id, price, pos.size, "SELL")
        if not ok:
            logger.warning(f"Exit SELL failed for {pos.team}")
            return
    await positions.exit_position(pos.id, price, reason)
