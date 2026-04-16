"""
config.py — Signal Harvest v4.1 Configuration
Three engines with sport-specific risk management.
Edge Finder operates as a PRE-GAME convergence trade (buy early, sell before game).
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
CHAIN_ID = 137

POLYMARKET_PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
POLYMARKET_FUNDER_ADDRESS = os.getenv("POLYMARKET_FUNDER_ADDRESS", "")
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "1"))

# ─── REDIS ───────────────────────────────────────────────────────────────────
REDIS_URL = os.getenv("REDIS_URL", "")

# ─── TIMING ──────────────────────────────────────────────────────────────────
HARVEST_INTERVAL = int(os.getenv("HARVEST_INTERVAL", "30"))
EDGE_SCAN_INTERVAL = int(os.getenv("EDGE_SCAN_INTERVAL", "120"))   # scan for new convergence trades
EDGE_EXIT_INTERVAL = int(os.getenv("EDGE_EXIT_INTERVAL", "30"))    # check exits FAST (30s)
ARBER_INTERVAL = int(os.getenv("ARBER_INTERVAL", "180"))
RESOLVE_INTERVAL = int(os.getenv("RESOLVE_INTERVAL", "120"))
API_PORT = int(os.getenv("PORT", os.getenv("API_PORT", "8080")))

# ─── RATE LIMITS ─────────────────────────────────────────────────────────────
MAX_ORDERS_PER_MINUTE = 55
FILL_CHECK_DELAY_MS = 500
MAX_UNFILLED_AGE = int(os.getenv("MAX_UNFILLED_AGE", "90"))

# ─── CAPITAL ALLOCATION ──────────────────────────────────────────────────────
MAX_TOTAL_EXPOSURE = float(os.getenv("MAX_TOTAL_EXPOSURE", "0.60"))
MAX_EDGE_EXPOSURE = float(os.getenv("MAX_EDGE_EXPOSURE", "0.40"))
MAX_ARBER_EXPOSURE = float(os.getenv("MAX_ARBER_EXPOSURE", "0.15"))

# ─── LIQUIDITY ───────────────────────────────────────────────────────────────
MIN_MARKET_LIQUIDITY = float(os.getenv("MIN_MARKET_LIQUIDITY", "500"))
MIN_DAILY_VOLUME = float(os.getenv("MIN_DAILY_VOLUME", "5000"))

# ─── KELLY SIZING ────────────────────────────────────────────────────────────
KELLY_FRACTION = float(os.getenv("KELLY_FRACTION", "0.25"))
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "0.10"))
MIN_TRADE_SIZE = float(os.getenv("MIN_TRADE_SIZE", "5"))

# ─── SPORT RISK TIERS (Kelly multipliers for Harvest) ────────────────────────
# Based on comeback probability research:
#   Tier 1: Soccer, NHL, NCAAF — blowouts almost never reverse
#   Tier 2: NFL, MLB — generally safe but occasional dramatic swings
#   Tier 3: NBA, NCAAB, WNBA — modern pace makes leads volatile
SPORT_RISK_MULTIPLIER = {
    # Tier 1 — safest (full Kelly) — all soccer + NHL + NCAAF
    "nhl": 1.0, "ncaaf": 1.0,
    **{s: 1.0 for s in [
        "epl", "liga", "seriea", "bundes", "ligue1", "mls", "ligamx", "ucl", "uel",
        "erediv", "liga2", "lig2fr", "bund2", "serieb", "porto", "scotpr", "uecl",
        "champ", "jleag", "j2", "aleag", "braA", "braB", "kleag", "china",
        "turk", "norw", "denm", "colom", "egypt", "libert", "sudam", "saudi",
    ]},
    # Tier 2 — standard (80% Kelly)
    "nfl": 0.8, "mlb": 0.8,
    # Tier 3 — cautious (50% Kelly) — high comeback risk
    "nba": 0.5, "ncaab": 0.5, "wnba": 0.5,
}

# ─── HARVEST ENGINE ──────────────────────────────────────────────────────────
HARVEST_MIN_CONFIDENCE = float(os.getenv("HARVEST_MIN_CONFIDENCE", "0.985"))  # raised from 0.975
HARVEST_MAX_PRICE = float(os.getenv("HARVEST_MAX_PRICE", "0.97"))
HARVEST_MIN_PRICE = float(os.getenv("HARVEST_MIN_PRICE", "0.80"))
HARVEST_MIN_EDGE = float(os.getenv("HARVEST_MIN_EDGE", "0.01"))

# ─── EDGE FINDER ENGINE (convergence trade) ──────────────────────────────────
EDGE_MIN_EDGE = float(os.getenv("EDGE_MIN_EDGE", "0.05"))           # 5% min mispricing
EDGE_MAX_PRICE = float(os.getenv("EDGE_MAX_PRICE", "0.75"))
EDGE_MIN_PRICE = float(os.getenv("EDGE_MIN_PRICE", "0.25"))
# Timing — only enter when game is 2-48 hours away
EDGE_MIN_HOURS_BEFORE = float(os.getenv("EDGE_MIN_HOURS_BEFORE", "2"))
EDGE_MAX_HOURS_BEFORE = float(os.getenv("EDGE_MAX_HOURS_BEFORE", "48"))
# Exit triggers
EDGE_EXIT_REMAINING = float(os.getenv("EDGE_EXIT_REMAINING", "0.02"))    # exit when edge < 2%
EDGE_STOP_LOSS = float(os.getenv("EDGE_STOP_LOSS", "0.05"))             # cut at 5¢ loss
EDGE_PRE_GAME_EXIT_MIN = int(os.getenv("EDGE_PRE_GAME_EXIT_MIN", "30")) # exit 30 min before game
EDGE_STALE_HOURS = float(os.getenv("EDGE_STALE_HOURS", "48"))           # exit if held >48h

# ─── POLY ARBER ENGINE ──────────────────────────────────────────────────────
ARBER_MIN_PROFIT = float(os.getenv("ARBER_MIN_PROFIT", "0.05"))      # raised from 3% to 5%
ARBER_MAX_OUTCOMES = int(os.getenv("ARBER_MAX_OUTCOMES", "10"))
ARBER_MIN_BET = float(os.getenv("ARBER_MIN_BET", "20"))               # minimum $20 per arb

# ─── ESPN SPORTS ─────────────────────────────────────────────────────────────
# 30+ leagues. Obscure soccer leagues are where pricing inefficiency is largest.
_S = {"slug": "soccer", "periods": 2, "period_name": "H"}  # soccer shorthand

ESPN_SPORTS = {
    # ── US Major Leagues ──
    "nba":    {"league": "nba",  "slug": "basketball",  "periods": 4, "period_name": "Q"},
    "wnba":   {"league": "wnba", "slug": "basketball",  "periods": 4, "period_name": "Q"},
    "nhl":    {"league": "nhl",  "slug": "hockey",      "periods": 3, "period_name": "P"},
    "mlb":    {"league": "mlb",  "slug": "baseball",    "periods": 9, "period_name": "Inn"},
    "nfl":    {"league": "nfl",  "slug": "football",    "periods": 4, "period_name": "Q"},
    "ncaab":  {"league": "mens-college-basketball", "slug": "basketball", "periods": 2, "period_name": "H"},
    "ncaaf":  {"league": "college-football", "slug": "football", "periods": 4, "period_name": "Q"},
    # ── Soccer: Tier 1 (high attention) ──
    "epl":    {"league": "eng.1",  **_S},
    "liga":   {"league": "esp.1",  **_S},
    "seriea": {"league": "ita.1",  **_S},
    "bundes": {"league": "ger.1",  **_S},
    "ligue1": {"league": "fra.1",  **_S},
    "ucl":    {"league": "uefa.champions", **_S},
    "uel":    {"league": "uefa.europa",    **_S},
    # ── Soccer: Tier 2 (moderate attention) ──
    "mls":    {"league": "usa.1",  **_S},
    "ligamx": {"league": "mex.1",  **_S},
    "erediv": {"league": "ned.1",  **_S},    # Eredivisie — 54 Polymarket markets
    "liga2":  {"league": "esp.2",  **_S},     # La Liga 2 — 45 markets
    "lig2fr": {"league": "fra.2",  **_S},     # Ligue 2 — 37 markets
    "bund2":  {"league": "ger.2",  **_S},     # 2. Bundesliga — 36 markets
    "serieb": {"league": "ita.2",  **_S},     # Serie B — 42 markets
    "porto":  {"league": "por.1",  **_S},     # Primeira Liga — 34 markets
    "scotpr": {"league": "sco.1",  **_S},     # Scottish Premiership
    "uecl":   {"league": "uefa.europa.conf", **_S},  # Conference League — 8 markets
    # ── Soccer: Tier 3 — SLEEPING LION territory (low attention, stale pricing) ──
    "champ":  {"league": "eng.2",  **_S},     # EFL Championship — 122 markets (beachboy4's goldmine)
    "jleag":  {"league": "jpn.1",  **_S},     # J-League — 115 markets
    "j2":     {"league": "jpn.2",  **_S},     # J2 League — 244 markets (MOST markets on Polymarket!)
    "aleag":  {"league": "aus.1",  **_S},     # A-League — 35 markets (Gray's backyard)
    "braA":   {"league": "bra.1",  **_S},     # Brazil Série A — 80 markets
    "braB":   {"league": "bra.2",  **_S},     # Brazil Série B — 81 markets
    "kleag":  {"league": "kor.1",  **_S},     # K-League — 80 markets
    "china":  {"league": "chn.1",  **_S},     # Chinese Super League — 96 markets
    "turk":   {"league": "tur.1",  **_S},     # Turkish Süper Lig — 53 markets
    "norw":   {"league": "nor.1",  **_S},     # Norway Eliteserien — 76 markets
    "denm":   {"league": "den.1",  **_S},     # Denmark Superliga — 60 markets
    "colom":  {"league": "col.1",  **_S},     # Colombia Primera A — 78 markets
    "egypt":  {"league": "egy.1",  **_S},     # Egypt Premier League — 102 markets
    "libert": {"league": "conmebol.libertadores", **_S},  # Copa Libertadores — 96 markets
    "sudam":  {"league": "conmebol.sudamericana", **_S},  # Copa Sudamericana — 96 markets
    "saudi":  {"league": "sau.1",  **_S},     # Saudi Pro League — 22 markets
}

# ─── WIN THRESHOLDS (research-calibrated) ────────────────────────────────────
# NBA: TIGHTENED — removed 10-point "safe" tier (32 twenty-point comebacks this season)
# Soccer: LOOSENED — added 2-goal at 60% tier (94% historical win rate at 2 goals)
WIN_THRESHOLDS = {
    "nba":    [(30,0.60,0.999,"blowout"),(25,0.75,0.998,"blowout"),(20,0.80,0.995,"strong"),(15,0.90,0.992,"strong")],
    "wnba":   [(25,0.60,0.998,"blowout"),(20,0.75,0.995,"blowout"),(15,0.85,0.992,"strong")],
    "nhl":    [(4,0.33,0.998,"blowout"),(3,0.50,0.995,"blowout"),(3,0.66,0.997,"strong"),(2,0.75,0.990,"strong"),(2,0.85,0.993,"safe")],
    "mlb":    [(7,0.66,0.998,"blowout"),(5,0.77,0.995,"blowout"),(4,0.77,0.990,"strong"),(3,0.85,0.985,"strong"),(3,0.88,0.990,"safe")],
    "nfl":    [(28,0.75,0.999,"blowout"),(21,0.80,0.997,"blowout"),(17,0.85,0.993,"strong"),(14,0.88,0.990,"strong"),(10,0.92,0.980,"safe")],
    "ncaab":  [(25,0.60,0.998,"blowout"),(20,0.75,0.995,"strong"),(15,0.85,0.990,"strong")],
    "ncaaf":  [(28,0.75,0.998,"blowout"),(21,0.80,0.995,"strong"),(14,0.88,0.990,"strong")],
    "soccer": [(3,0.40,0.997,"blowout"),(2,0.60,0.990,"strong"),(2,0.75,0.993,"safe")],
}

SOCCER_SPORTS = {
    "epl", "liga", "seriea", "bundes", "ligue1", "mls", "ligamx", "ucl", "uel",
    "erediv", "liga2", "lig2fr", "bund2", "serieb", "porto", "scotpr", "uecl",
    "champ", "jleag", "j2", "aleag", "braA", "braB", "kleag", "china",
    "turk", "norw", "denm", "colom", "egypt", "libert", "sudam", "saudi",
}

# ─── POLYMARKET TAG SLUGS ────────────────────────────────────────────────────
SPORT_TAG_SLUGS = {
    # ── US Major ──
    "nba": "nba", "wnba": "wnba", "nhl": "nhl", "mlb": "mlb",
    "nfl": "nfl", "ncaab": "ncaa-basketball", "ncaaf": "ncaa-football",
    # ── Soccer Tier 1 ──
    "epl": "epl", "liga": "la-liga", "seriea": "serie-a",
    "bundes": "bundesliga", "ligue1": "ligue-1",
    "ucl": "champions-league", "uel": "europa-league",
    # ── Soccer Tier 2 ──
    "mls": "mls", "ligamx": "liga-mx",
    "erediv": "eredivisie", "liga2": "la-liga-2", "lig2fr": "ligue-2",
    "bund2": "2-bundesliga", "serieb": "serie-b", "porto": "primeira-liga",
    "scotpr": "scottish-premiership", "uecl": "europa-conference-league",
    # ── Soccer Tier 3 — Sleeping Lion ──
    "champ": "efl-championship", "jleag": "japan-j-league", "j2": "j2-league",
    "aleag": "a-league-soccer", "braA": "brazil-serie-a", "braB": "brazil-serie-b",
    "kleag": "k-league", "china": "chinese-super-league",
    "turk": "super-lig", "norw": "norway-eliteserien",
    "denm": "denmark-superliga", "colom": "colombia-primera-a",
    "egypt": "egypt-premier-league",
    "libert": "copa-libertadores", "sudam": "copa-sudamericana",
    "saudi": "saudi-professional-league",
}

# ─── POLYMARKET SERIES IDs ───────────────────────────────────────────────────
# From official /sports endpoint. This is THE correct way to fetch game markets.
# Combined with tag_id=100639 = games-only (no futures).
POLY_SERIES_IDS = {
    # US Major
    "nba": "10345", "nhl": "10346", "mlb": "3",
    "nfl": "10187", "wnba": "10105",
    "ncaab": "10470",  # D1 college basketball (men)
    "ncaaf": "10210",  # college football
    # Soccer Tier 1 — Big European leagues
    "epl": "10188", "liga": "10193", "seriea": "10203",
    "bundes": "10194", "ligue1": "10195",
    "ucl": "10204", "uel": "10209",
    # Soccer Tier 2
    "mls": "10189", "ligamx": "10290",
    "erediv": "10286", "porto": "10330",
    "uecl": "10437",
    # Soccer Tier 3 — Sleeping Lion (THE goldmine)
    "champ": "10230",    # EFL Championship
    "jleag": "10360",    # J-League
    "j2": "10443",       # J2 League (244 markets!)
    "aleag": "10438",    # A-League (Brisbane home 🇦🇺)
    "braA": "10359",     # Brazil Série A
    "kleag": "10444",    # K-League
    "china": "10439",    # Chinese Super League
    "turk": "10292",     # Turkish Süper Lig
    "norw": "10362",     # Norway Eliteserien
    "denm": "10363",     # Denmark Superliga
    "saudi": "10361",    # Saudi Pro League
    "libert": "10289",   # Copa Libertadores
    "sudam": "10291",    # Copa Sudamericana
}

# Tag ID 100639 = game markets only (filters out futures/season-long bets)
POLY_GAMES_TAG_ID = "100639"

# ─── SLEEPING LION — Low-attention market multiplier for Edge Finder ─────────
# Markets with less retail attention have staler pricing = bigger edge.
# beachboy4 made $4.14M from 149 bets targeting exactly these leagues.
# Multiplier applied to Edge Finder Kelly sizing (1.0 = normal, 1.5 = 50% bigger bets)
SLEEPING_LION = {
    # Tier 1 — high attention, efficient pricing (normal sizing)
    "epl": 1.0, "liga": 1.0, "seriea": 1.0, "bundes": 1.0, "ligue1": 1.0,
    "ucl": 1.0, "uel": 1.0,
    "nba": 1.0, "nfl": 1.0, "mlb": 1.0, "nhl": 1.0,
    "ncaab": 1.0, "ncaaf": 1.0, "wnba": 1.0,
    # Tier 2 — moderate attention (20% bigger bets)
    "mls": 1.2, "ligamx": 1.2, "erediv": 1.2, "porto": 1.2,
    "liga2": 1.2, "lig2fr": 1.2, "bund2": 1.2, "serieb": 1.2,
    "scotpr": 1.2, "uecl": 1.2,
    # Tier 3 — Sleeping Lion: stale pricing, big edge (50% bigger bets) 🦁
    "champ": 1.5,   # beachboy4's Sunderland bet: $1.29M → $1.86M profit
    "jleag": 1.5,   # J-League: 115 markets, minimal retail attention
    "j2": 1.5,      # J2 League: 244 markets, LEAST efficient on Polymarket
    "aleag": 1.5,   # A-League: your backyard advantage
    "braA": 1.5, "braB": 1.5,   # Brazilian leagues: huge market count, low attention
    "kleag": 1.5,   # K-League: 80 markets
    "china": 1.5,   # Chinese Super League: 96 markets
    "turk": 1.3,    # Turkish Süper Lig: moderate
    "norw": 1.5, "denm": 1.5,   # Nordic leagues
    "colom": 1.5,   # Colombia Primera A: 78 markets
    "egypt": 1.5,   # Egypt Premier League: 102 markets
    "libert": 1.3, "sudam": 1.3,  # South American cups: decent attention
    "saudi": 1.3,   # Saudi Pro League
}

# ─── EDGE-LADDERED SIZING ────────────────────────────────────────────────────
# Bigger edges deserve bigger bets. 15% edge is way more certain than 5% edge.
# Applied as multiplier to Kelly bet size in Edge Finder.
EDGE_SIZE_LADDER = [
    # (min_edge, multiplier)
    (0.15, 2.0),    # 15%+ edge: double Kelly — rare, high conviction (beachboy4 territory)
    (0.10, 1.5),    # 10-15% edge: 1.5× Kelly — strong mispricing
    (0.07, 1.2),    # 7-10% edge: 1.2× Kelly — solid edge
    (0.05, 1.0),    # 5-7% edge: standard Kelly — minimum threshold
]

# ─── FUTURES / PROPS BLOCK ───────────────────────────────────────────────────
FUTURES_BLOCK = {
    "champion", "mvp", "winner", "playoff", "series", "division",
    "conference", "prop", "total points", "over/under", "spread",
    "most", "season", "award", "draft", "futures",
    "rookie", "all-star", "leader", "scoring", "outright",
    "relegation", "relegated", "golden boot", "ballon", "transfer",
    "finish", "place", "top 4", "top 6", "top half", "bottom",
    "promoted", "promotion", "survive", "survival",
    "which club", "which team", "who will", "make playoffs",
    "win total", "over under", "regular season",
}

# ─── DASHBOARD ───────────────────────────────────────────────────────────────
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "https://signal-sports-dashboard.vercel.app,http://localhost:5173").split(",")
