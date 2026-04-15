"""
config.py — Signal Harvest + Synth Bot v2

Two engines, one bankroll:
  ENGINE 1 (Harvest): ESPN verified sports blowouts → Polymarket
  ENGINE 2 (Synth):   Bittensor SN50 crypto predictions → Polymarket

Both compound into the same equity pool.
"""

import os
from dotenv import load_dotenv
load_dotenv()

# ═══ MODE ═══
PAPER_MODE = os.getenv("PAPER_MODE", "true").lower() == "true"

# ═══ POLYMARKET ═══
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
FUNDER_ADDRESS = os.getenv("POLYMARKET_FUNDER_ADDRESS", "")

# ═══ BANKROLL ═══
STARTING_BANKROLL = float(os.getenv("STARTING_BANKROLL", "1000"))

# ═══ GLOBAL RISK ═══
MAX_TOTAL_EXPOSURE_PCT = 0.80      # 80% of equity across BOTH engines
MIN_SHARES = 5

# ═══ KELLY CRITERION ═══
KELLY_FRACTION = 0.25              # Quarter-Kelly — aggressive but not reckless
KELLY_MAX_BET_PCT = 0.12           # Never bet more than 12% of equity on one trade
KELLY_MIN_EDGE = 0.02              # Minimum edge to trade (EV > 2% of cost)

# ════════════════════════════════════════════
# ENGINE 1: HARVEST (ESPN Sports)
# ════════════════════════════════════════════
HARVEST_ENABLED = os.getenv("HARVEST_ENABLED", "true").lower() == "true"
HARVEST_POSITION_PCT = 0.10        # 10% of equity per harvest
HARVEST_MAX_USD = 150.0
HARVEST_MAX_EXPOSURE_PCT = 0.50    # 50% of equity in harvest positions
HARVEST_MIN_VOLUME = 500

# Price thresholds
HARVEST_VERIFIED_MIN = 0.85
HARVEST_VERIFIED_MAX = 0.97
HARVEST_UNVERIFIED_MIN = 0.93
HARVEST_MIN_RETURN = 0.025

# Harvest uses CLOB for real prices too
HARVEST_USE_CLOB = True            # Query real orderbook for sports markets

# ESPN (free, no key)
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"
ESPN_SPORTS = {
    "nba":   {"url": f"{ESPN_BASE}/basketball/nba/scoreboard",         "poly_tags": ["nba", "basketball"]},
    "nhl":   {"url": f"{ESPN_BASE}/hockey/nhl/scoreboard",             "poly_tags": ["nhl"]},
    "mlb":   {"url": f"{ESPN_BASE}/baseball/mlb/scoreboard",           "poly_tags": ["mlb", "baseball"]},
    "nfl":   {"url": f"{ESPN_BASE}/football/nfl/scoreboard",           "poly_tags": ["nfl"]},
    "ncaab": {"url": f"{ESPN_BASE}/basketball/mens-college-basketball/scoreboard", "poly_tags": ["college-basketball"]},
    "ncaaf": {"url": f"{ESPN_BASE}/football/college-football/scoreboard",          "poly_tags": ["college-football"]},
    "wnba":  {"url": f"{ESPN_BASE}/basketball/wnba/scoreboard",       "poly_tags": ["wnba"]},
    "epl":   {"url": f"{ESPN_BASE}/soccer/eng.1/scoreboard",          "poly_tags": ["epl", "soccer"]},
    "mls":   {"url": f"{ESPN_BASE}/soccer/usa.1/scoreboard",          "poly_tags": ["mls"]},
    "liga":  {"url": f"{ESPN_BASE}/soccer/esp.1/scoreboard",          "poly_tags": ["la-liga"]},
}

# Win probability thresholds: (min_lead, min_elapsed, confidence, level)
WIN_THRESHOLDS = {
    "nba":    [(30,0.60,0.999,"blowout"),(25,0.75,0.998,"blowout"),(20,0.80,0.995,"strong"),(15,0.85,0.992,"strong"),(12,0.90,0.985,"safe"),(10,0.92,0.975,"safe")],
    "nhl":    [(4,0.33,0.998,"blowout"),(3,0.50,0.995,"blowout"),(3,0.66,0.997,"strong"),(2,0.75,0.990,"strong"),(2,0.85,0.993,"safe")],
    "mlb":    [(7,0.66,0.998,"blowout"),(5,0.77,0.995,"blowout"),(4,0.77,0.990,"strong"),(3,0.85,0.985,"strong"),(3,0.88,0.990,"safe")],
    "nfl":    [(28,0.75,0.999,"blowout"),(21,0.80,0.997,"blowout"),(17,0.85,0.993,"strong"),(14,0.88,0.990,"strong"),(10,0.92,0.980,"safe")],
    "ncaab":  [(25,0.60,0.998,"blowout"),(20,0.75,0.995,"strong"),(15,0.85,0.990,"strong"),(10,0.90,0.980,"safe")],
    "ncaaf":  [(28,0.75,0.998,"blowout"),(21,0.80,0.995,"strong"),(14,0.88,0.990,"strong")],
    "soccer": [(3,0.50,0.995,"blowout"),(2,0.66,0.985,"strong"),(2,0.83,0.992,"safe")],
}

# Futures keywords to block
FUTURES_BLOCK = {
    "champion","end of 2025","end of 2026","end of 2027","pound-for-pound",
    "fight next","next fight","become a","mvp","rookie of","award","season",
    "playoff","finals winner","world series winner","super bowl winner",
    "stanley cup winner","title","will make","next manager","next coach",
    "ballon d'or","heisman","cy young","all-star","draft","transfer",
}

# ════════════════════════════════════════════
# ENGINE 2: SYNTH (Bittensor SN50 Crypto)
# ════════════════════════════════════════════
SYNTH_ENABLED = os.getenv("SYNTH_ENABLED", "true").lower() == "true"
SYNTH_API_KEY = os.getenv("SYNTH_API_KEY", "")
SYNTH_BASE = "https://api.synthdata.co"

# Position sizing per timeframe (Kelly overrides pct when edge is known)
SYNTH_SIZING = {
    "5min":  {"pct": 0.06, "max_usd": 80,  "min_edge": 0.03},   # 6% equity — highest conviction
    "15min": {"pct": 0.04, "max_usd": 60,  "min_edge": 0.05},   # 4% equity, small & frequent
    "hourly": {"pct": 0.06, "max_usd": 100, "min_edge": 0.07},   # 6% equity, medium
    "daily":  {"pct": 0.08, "max_usd": 120, "min_edge": 0.08},   # 8% equity, big conviction
}
SYNTH_MAX_EXPOSURE_PCT = 0.40      # 40% of equity in synth positions
SYNTH_ASSETS = ["BTC", "ETH", "SOL"]

# ═══ ARB DETECTION ═══
ARB_ENABLED = True                 # Layer 3: pair arbitrage
ARB_MAX_COMBINED = 0.97            # Buy both sides if YES+NO < this
ARB_MAX_USD = 50                   # Cap per arb

# ═══ SMART SCAN TIMING ═══
# Instead of fixed intervals, the synth engine calculates exact sleep times
# to wake up at the optimal moment before each window close.
SNIPE_LEAD_TIME = 20               # Wake up this many seconds before window close
SNIPE_SCAN_BURST = 3               # Seconds between scans during the hot zone

# ═══ SCAN INTERVALS ═══
HARVEST_SCAN_INTERVAL = int(os.getenv("HARVEST_INTERVAL", "90"))
SYNTH_SCAN_INTERVAL = int(os.getenv("SYNTH_INTERVAL", "15"))   # Fallback only — smart timing overrides

# ═══ RESOLUTION ═══
CRYPTO_RESOLVE_BUFFER = 20         # Seconds after window close to resolve (was 60)

# ═══ PERSISTENCE ═══
# Redis URL for state persistence across Railway deploys
# Free tier: https://upstash.com (or Railway Redis addon)
REDIS_URL = os.getenv("REDIS_URL", "")

# ═══ API SERVER ═══
API_AUTH_TOKEN = os.getenv("API_AUTH_TOKEN", "")

# ═══ LOGGING ═══
LOG_DIR = os.getenv("LOG_DIR", "./logs")
TRADE_LOG = os.path.join(LOG_DIR, "trades.jsonl")
BANKROLL_LOG = os.path.join(LOG_DIR, "bankroll.jsonl")
