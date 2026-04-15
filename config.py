"""
config.py — Signal Harvest Bot Configuration
All settings for both engines, execution params, and deployment config.
Every value configurable via env var with sane production defaults.
"""

import os

# ─── MODE ────────────────────────────────────────────────────────────────────
PAPER_MODE = os.getenv("PAPER_MODE", "true").lower() == "true"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# ─── BANKROLL ────────────────────────────────────────────────────────────────
STARTING_BANKROLL = float(os.getenv("STARTING_BANKROLL", "1000"))

# ─── POLYMARKET CLOB ─────────────────────────────────────────────────────────
CLOB_HOST = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CHAIN_ID = 137  # Polygon

POLYMARKET_PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
POLYMARKET_FUNDER_ADDRESS = os.getenv("POLYMARKET_FUNDER_ADDRESS", "")
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "1"))  # 1=POLY_PROXY, 0=EOA

# ─── THE ODDS API (Sharp Edge engine) ────────────────────────────────────────
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
ODDS_API_BASE = "https://api.the-odds-api.com/v4/sports"

# ─── REDIS PERSISTENCE (optional, falls back to file) ────────────────────────
REDIS_URL = os.getenv("REDIS_URL", "")

# ─── TIMING ──────────────────────────────────────────────────────────────────
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "45"))
HARVEST_INTERVAL = int(os.getenv("HARVEST_INTERVAL", "30"))
SHARP_INTERVAL = int(os.getenv("SHARP_INTERVAL", "900"))   # 15 min — rotates 1 sport per scan
RESOLVE_INTERVAL = int(os.getenv("RESOLVE_INTERVAL", "120"))
API_PORT = int(os.getenv("PORT", os.getenv("API_PORT", "8080")))

# ─── RATE LIMITS ─────────────────────────────────────────────────────────────
MAX_ORDERS_PER_MINUTE = 55
FILL_CHECK_DELAY_MS = 500
MAX_UNFILLED_AGE = int(os.getenv("MAX_UNFILLED_AGE", "90"))

# ─── HARVEST ENGINE (Engine 1) ───────────────────────────────────────────────
HARVEST_MIN_CONFIDENCE = float(os.getenv("HARVEST_MIN_CONFIDENCE", "0.975"))
HARVEST_MAX_PRICE = float(os.getenv("HARVEST_MAX_PRICE", "0.97"))
HARVEST_MIN_PRICE = float(os.getenv("HARVEST_MIN_PRICE", "0.80"))
HARVEST_MIN_EDGE = float(os.getenv("HARVEST_MIN_EDGE", "0.01"))

# ─── SHARP EDGE ENGINE (Engine 2) ────────────────────────────────────────────
SHARP_MIN_EDGE = float(os.getenv("SHARP_MIN_EDGE", "0.03"))
SHARP_MAX_PRICE = float(os.getenv("SHARP_MAX_PRICE", "0.70"))
SHARP_MIN_PRICE = float(os.getenv("SHARP_MIN_PRICE", "0.30"))
SHARP_MIN_PINNACLE_PROB = float(os.getenv("SHARP_MIN_PINNACLE_PROB", "0.35"))

# Quota math: Free = 500 req/month. We rotate 1 sport per 15-min scan.
# 96 scans/day × 1 call = 96/day. But we cap at 16 calls/day = 480/month.
SHARP_MAX_CALLS_PER_DAY = int(os.getenv("SHARP_MAX_CALLS_PER_DAY", "16"))

# ─── KELLY SIZING ────────────────────────────────────────────────────────────
KELLY_FRACTION = float(os.getenv("KELLY_FRACTION", "0.25"))
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "0.10"))
MIN_TRADE_SIZE = float(os.getenv("MIN_TRADE_SIZE", "5"))
MAX_TRADE_SIZE = float(os.getenv("MAX_TRADE_SIZE", "100"))

# ─── ESPN SPORTS CONFIG ──────────────────────────────────────────────────────
ESPN_SPORTS = {
    "nba":    {"league": "nba",  "slug": "basketball",  "periods": 4, "period_name": "Q"},
    "wnba":   {"league": "wnba", "slug": "basketball",  "periods": 4, "period_name": "Q"},
    "nhl":    {"league": "nhl",  "slug": "hockey",      "periods": 3, "period_name": "P"},
    "mlb":    {"league": "mlb",  "slug": "baseball",    "periods": 9, "period_name": "Inn"},
    "nfl":    {"league": "nfl",  "slug": "football",    "periods": 4, "period_name": "Q"},
    "ncaab":  {"league": "mens-college-basketball", "slug": "basketball", "periods": 2, "period_name": "H"},
    "ncaaf":  {"league": "college-football", "slug": "football", "periods": 4, "period_name": "Q"},
    "epl":    {"league": "eng.1",  "slug": "soccer", "periods": 2, "period_name": "H"},
    "liga":   {"league": "esp.1",  "slug": "soccer", "periods": 2, "period_name": "H"},
    "seriea": {"league": "ita.1",  "slug": "soccer", "periods": 2, "period_name": "H"},
    "bundes": {"league": "ger.1",  "slug": "soccer", "periods": 2, "period_name": "H"},
    "ligue1": {"league": "fra.1",  "slug": "soccer", "periods": 2, "period_name": "H"},
    "mls":    {"league": "usa.1",  "slug": "soccer", "periods": 2, "period_name": "H"},
    "ligamx": {"league": "mex.1",  "slug": "soccer", "periods": 2, "period_name": "H"},
    "ucl":    {"league": "uefa.champions", "slug": "soccer", "periods": 2, "period_name": "H"},
    "uel":    {"league": "uefa.europa",    "slug": "soccer", "periods": 2, "period_name": "H"},
}

# ─── WIN THRESHOLDS ──────────────────────────────────────────────────────────
WIN_THRESHOLDS = {
    "nba":    [(30,0.60,0.999,"blowout"),(25,0.75,0.998,"blowout"),(20,0.80,0.995,"strong"),(15,0.85,0.992,"strong"),(12,0.90,0.985,"safe"),(10,0.92,0.975,"safe")],
    "wnba":   [(25,0.60,0.998,"blowout"),(20,0.75,0.995,"blowout"),(15,0.80,0.992,"strong"),(12,0.85,0.985,"strong"),(10,0.90,0.975,"safe")],
    "nhl":    [(4,0.33,0.998,"blowout"),(3,0.50,0.995,"blowout"),(3,0.66,0.997,"strong"),(2,0.75,0.990,"strong"),(2,0.85,0.993,"safe")],
    "mlb":    [(7,0.66,0.998,"blowout"),(5,0.77,0.995,"blowout"),(4,0.77,0.990,"strong"),(3,0.85,0.985,"strong"),(3,0.88,0.990,"safe")],
    "nfl":    [(28,0.75,0.999,"blowout"),(21,0.80,0.997,"blowout"),(17,0.85,0.993,"strong"),(14,0.88,0.990,"strong"),(10,0.92,0.980,"safe")],
    "ncaab":  [(25,0.60,0.998,"blowout"),(20,0.75,0.995,"strong"),(15,0.85,0.990,"strong"),(10,0.90,0.980,"safe")],
    "ncaaf":  [(28,0.75,0.998,"blowout"),(21,0.80,0.995,"strong"),(14,0.88,0.990,"strong")],
    "soccer": [(3,0.50,0.995,"blowout"),(2,0.66,0.985,"strong"),(2,0.83,0.992,"safe")],
}

# Soccer leagues share the "soccer" threshold key
SOCCER_SPORTS = {"epl", "liga", "seriea", "bundes", "ligue1", "mls", "ligamx", "ucl", "uel"}

# ─── POLYMARKET TAG SLUGS ────────────────────────────────────────────────────
SPORT_TAG_SLUGS = {
    "nba": "nba", "wnba": "wnba", "nhl": "nhl", "mlb": "mlb",
    "nfl": "nfl", "ncaab": "ncaa-basketball", "ncaaf": "ncaa-football",
    "epl": "epl", "liga": "la-liga", "seriea": "serie-a",
    "bundes": "bundesliga", "ligue1": "ligue-1", "mls": "mls",
    "ligamx": "liga-mx", "ucl": "champions-league", "uel": "europa-league",
}

# ─── ODDS API SPORT KEYS ────────────────────────────────────────────────────
ODDS_SPORT_KEYS = {
    "nba": "basketball_nba",
    "wnba": "basketball_wnba",
    "nhl": "icehockey_nhl",
    "mlb": "baseball_mlb",
    "nfl": "americanfootball_nfl",
    "ncaab": "basketball_ncaab",
    "ncaaf": "americanfootball_ncaaf",
    "epl": "soccer_epl",
    "liga": "soccer_spain_la_liga",
    "seriea": "soccer_italy_serie_a",
    "bundes": "soccer_germany_bundesliga",
    "ligue1": "soccer_france_ligue_one",
    "mls": "soccer_usa_mls",
    "ligamx": "soccer_mexico_ligamx",
    "ucl": "soccer_uefa_champs_league",
    "uel": "soccer_uefa_europa_league",
}

# ─── FUTURES / PROPS BLOCK LIST ──────────────────────────────────────────────
# Removed "first" and "top" — they appear in legitimate game titles
FUTURES_BLOCK = {
    "champion", "mvp", "winner", "playoff", "series", "division",
    "conference", "prop", "total points", "over/under", "spread",
    "most", "season", "award", "draft", "futures",
    "rookie", "all-star", "leader", "scoring", "outright",
    "relegation", "golden boot", "ballon", "transfer",
}

# ─── DASHBOARD ───────────────────────────────────────────────────────────────
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "https://signal-sports-dashboard.vercel.app,http://localhost:5173").split(",")
