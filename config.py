"""
config.py — Signal Harvest Bot v3

Sports blowout harvester with live Polymarket execution.
Maker limit orders only (zero fees + rebates).

Architecture:
  ESPN live scores → blowout detection → Polymarket CLOB match →
  real orderbook price → EV check → maker limit order → hold to resolution
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
# 0=EOA (own wallet), 1=POLY_PROXY (MagicLink/email), 2=GNOSIS_SAFE
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "1"))

# ═══ BANKROLL ═══
STARTING_BANKROLL = float(os.getenv("STARTING_BANKROLL", "1000"))

# ═══ RISK MANAGEMENT ═══
MAX_EXPOSURE_PCT = 0.60            # Never expose more than 60% of equity
MAX_SINGLE_BET_PCT = 0.10          # Never bet more than 10% on one trade
MAX_SINGLE_BET_USD = 150.0         # Hard USD cap per trade
MIN_SHARES = 5                     # Minimum order size (CLOB minimum)
MIN_EV = 0.015                     # Minimum EV per share to trade ($0.015)

# ═══ KELLY CRITERION ═══
KELLY_FRACTION = 0.25              # Quarter-Kelly
KELLY_MAX_PCT = 0.10               # Cap at 10% even if Kelly says more

# ═══ HARVEST SETTINGS ═══
# Price gates — only buy shares in this range
PRICE_MIN = 0.85                   # Don't buy below 85¢ (too much risk)
PRICE_MAX = 0.97                   # Don't buy above 97¢ (not enough return)
MIN_RETURN = 0.025                 # Minimum implied return (2.5%)
MIN_VOLUME = 500                   # Minimum market volume (liquidity filter)
MAX_SPREAD = 0.06                  # Skip if CLOB spread > 6%

# ═══ ESPN (free, no key) ═══
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

# Futures/props keywords to block (we only want moneyline game outcomes)
FUTURES_BLOCK = {
    "champion","end of 2025","end of 2026","end of 2027","pound-for-pound",
    "fight next","next fight","become a","mvp","rookie of","award","season",
    "playoff","finals winner","world series winner","super bowl winner",
    "stanley cup winner","title","will make","next manager","next coach",
    "ballon d'or","heisman","cy young","all-star","draft","transfer",
}

# ═══ ORDER EXECUTION ═══
ORDER_TIMEOUT = 90                 # Cancel unfilled limit orders after 90s
FILL_POLL_INTERVAL = 5             # Poll for fills every 5s
POST_FILL_DELAY = 0.5              # Wait 500ms after fill before balance check
RATE_LIMIT_PER_MIN = 55            # Stay under CLOB's 60/min limit

# ═══ SCAN TIMING ═══
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "60"))  # Seconds between scans
RESOLUTION_INTERVAL = 120          # Check for resolved markets every 2min

# ═══ PERSISTENCE ═══
REDIS_URL = os.getenv("REDIS_URL", "")
STATE_FILE = os.getenv("STATE_FILE", "./data/state.json")
TRADE_LOG = os.getenv("TRADE_LOG", "./data/trades.jsonl")

# ═══ API SERVER ═══
API_PORT = int(os.getenv("PORT", "3001"))
