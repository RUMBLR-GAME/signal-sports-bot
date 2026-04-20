"""
clv_gate.py — Validates paper CLV before allowing live mode.

The only real proof of alpha in sports betting is positive Closing Line Value
(CLV). We track CLV at pre-game exit (T-30min) by comparing our entry price to
the sharp bookmaker's closing line. If avg CLV > 0 across a large enough sample,
we have statistically proven alpha.

This module exposes:
  - evaluate_clv_gate(positions, bot_state) → dict describing pass/fail/progress
  - live_mode_allowed(positions) → bool (safe to trade real money?)

Used by main.py to flip to paper mode on startup if CLV hasn't been validated.
Can be bypassed with CLV_GATE_BYPASS=true for emergencies.
"""
import logging
import time
from typing import Optional

from config import (
    CLV_GATE_ENABLED, CLV_GATE_MIN_SAMPLES, CLV_GATE_MIN_AVG, CLV_GATE_BYPASS,
    PAPER_MODE,
)

logger = logging.getLogger("clv_gate")


def collect_clv_samples(positions) -> list:
    """Return list of (clv_edge, closed_at) for all trades with CLV data."""
    samples = []
    for t in positions.trades:
        clv = getattr(t, "clv_edge", None)
        if clv is None:
            continue
        try:
            clv_f = float(clv)
        except (TypeError, ValueError):
            continue
        samples.append({
            "clv_edge": clv_f,
            "closed_at": getattr(t, "closed_at", 0),
            "team": getattr(t, "team", ""),
            "sport": getattr(t, "sport", ""),
        })
    return samples


def evaluate_clv_gate(positions) -> dict:
    """Analyze CLV data and return gate status.
    Returns a dict with keys:
      enabled: is the gate active
      samples: number of CLV-tracked trades
      required: required sample count
      avg_clv: average CLV across samples (None if no samples)
      required_avg: minimum avg CLV to pass
      passes: bool — whether gate allows live mode
      reason: human-readable status
      progress_pct: 0-100 toward passing (useful for dashboard)
      last_sample_at: timestamp of most recent CLV sample (staleness check)
    """
    if not CLV_GATE_ENABLED:
        return {
            "enabled": False,
            "passes": True,
            "reason": "CLV gate disabled (CLV_GATE_ENABLED=false)",
            "samples": 0,
            "required": 0,
            "avg_clv": None,
            "required_avg": 0.0,
            "progress_pct": 100,
            "last_sample_at": None,
        }

    if CLV_GATE_BYPASS:
        return {
            "enabled": True,
            "passes": True,
            "reason": "CLV gate BYPASSED (CLV_GATE_BYPASS=true) — risky",
            "samples": 0,
            "required": CLV_GATE_MIN_SAMPLES,
            "avg_clv": None,
            "required_avg": CLV_GATE_MIN_AVG,
            "progress_pct": 100,
            "last_sample_at": None,
        }

    samples = collect_clv_samples(positions)
    n = len(samples)
    avg = None
    last_at = None
    if samples:
        avg = sum(s["clv_edge"] for s in samples) / n
        last_at = max(s["closed_at"] for s in samples)

    # Dual gate: enough samples AND avg above threshold
    has_enough_samples = n >= CLV_GATE_MIN_SAMPLES
    meets_threshold = avg is not None and avg >= CLV_GATE_MIN_AVG

    # Progress toward pass (for dashboard)
    sample_progress = min(100, int(100 * n / max(CLV_GATE_MIN_SAMPLES, 1)))
    if avg is None:
        avg_progress = 0
    elif avg >= CLV_GATE_MIN_AVG:
        avg_progress = 100
    elif avg < 0:
        avg_progress = 0
    else:
        avg_progress = int(100 * avg / max(CLV_GATE_MIN_AVG, 0.001))
    overall_progress = min(sample_progress, avg_progress) if meets_threshold else sample_progress // 2

    passes = has_enough_samples and meets_threshold

    if passes:
        reason = f"CLV validated: {n} samples, avg {avg*100:+.2f}¢ ≥ threshold {CLV_GATE_MIN_AVG*100:+.2f}¢"
    elif not has_enough_samples and avg is None:
        reason = f"No CLV data yet (need {CLV_GATE_MIN_SAMPLES} samples with CLV tracking)"
    elif not has_enough_samples:
        reason = f"Only {n}/{CLV_GATE_MIN_SAMPLES} samples, avg so far {avg*100:+.2f}¢"
    elif avg is None:
        reason = "Samples exist but no CLV readings — check pre-game exit logic"
    else:
        reason = f"CLV avg {avg*100:+.2f}¢ < required {CLV_GATE_MIN_AVG*100:+.2f}¢ ({n} samples)"

    return {
        "enabled": True,
        "passes": passes,
        "reason": reason,
        "samples": n,
        "required": CLV_GATE_MIN_SAMPLES,
        "avg_clv": avg,
        "required_avg": CLV_GATE_MIN_AVG,
        "progress_pct": overall_progress,
        "last_sample_at": last_at,
    }


def live_mode_allowed(positions) -> bool:
    """Returns True if live mode is safe to activate based on CLV."""
    status = evaluate_clv_gate(positions)
    return status["passes"]


def log_gate_status_on_startup(positions) -> None:
    """Log a clear message at startup about CLV gate state."""
    status = evaluate_clv_gate(positions)
    if not status["enabled"]:
        logger.info("CLV gate: DISABLED")
        return
    marker = "✓ PASS" if status["passes"] else "✗ BLOCKED"
    logger.info(f"CLV gate: {marker}  {status['reason']}")
    if not PAPER_MODE and not status["passes"]:
        logger.error("=" * 55)
        logger.error("  LIVE MODE REQUESTED BUT CLV GATE NOT PASSED")
        logger.error(f"  {status['reason']}")
        logger.error("  To override: set CLV_GATE_BYPASS=true")
        logger.error("  Bot will refuse to place live orders until gate passes")
        logger.error("=" * 55)
