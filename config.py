"""
config.py — Signal Harvest v18 Configuration
Fixes the compounding bug (MAX_TOTAL_EXPOSURE now scales with equity, not fixed bankroll).
Adds: drawdown governor, circuit breaker, correlation caps, partial exits,
Odds API (flag-gated), NBA safety filter.
"""
import os


def _bool(key, default=False):
    v = os.getenv(key, "")
    if not v:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _flt(key, default):
    try:
        return float(os.getenv(key, default))
    except (TypeError, ValueError):
        return float(default)


def _int(key, default):
    try:
        return int(os.getenv(key, default))
    except (TypeError, ValueError):
        return int(default)


# ─── MODE ────────────────────────────────────────────────────────────────────
PAPER_MODE = _bool("PAPER_MODE", True)
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
FORCE_RESET = _bool("FORCE_RESET", False)

# ─── BANKROLL ────────────────────────────────────────────────────────────────
STARTING_BANKROLL = _flt("STARTING_BANKROLL", 1000)

# ─── POLYMARKET ──────────────────────────────────────────────────────────────
CLOB_HOST = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CHAIN_ID = 137

POLYMARKET_PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
POLYMARKET_FUNDER_ADDRESS = os.getenv("POLYMARKET_FUNDER_ADDRESS", "")
SIGNATURE_TYPE = _int("SIGNATURE_TYPE", 1)

# ─── REDIS ───────────────────────────────────────────────────────────────────
REDIS_URL = os.getenv("REDIS_URL", "")

# ─── TIMING ──────────────────────────────────────────────────────────────────
HARVEST_INTERVAL = _int("HARVEST_INTERVAL", 30)
EDGE_SCAN_INTERVAL = _int("EDGE_SCAN_INTERVAL", 120)
EDGE_EXIT_INTERVAL = _int("EDGE_EXIT_INTERVAL", 30)
RESOLVE_INTERVAL = _int("RESOLVE_INTERVAL", 120)
PARTIAL_CHECK_INTERVAL = _int("PARTIAL_CHECK_INTERVAL", 60)
EQUITY_CURVE_INTERVAL = _int("EQUITY_CURVE_INTERVAL", 60)  # record point every N sec
API_PORT = _int("PORT", _int("API_PORT", 8080))

# ─── RATE LIMITS ─────────────────────────────────────────────────────────────
MAX_ORDERS_PER_MINUTE = 55
FILL_CHECK_DELAY_MS = 500
MAX_UNFILLED_AGE = _int("MAX_UNFILLED_AGE", 90)

# ─── EXPOSURE CAPS (THE COMPOUNDING FIX) ─────────────────────────────────────
# v17 bug: capped at 60% of STARTING_BANKROLL forever → couldn't compound.
# v18: percentages of CURRENT equity. Now actually compounds.
MAX_TOTAL_EXPOSURE = _flt("MAX_TOTAL_EXPOSURE", 0.60)       # 60% of equity
MAX_EDGE_EXPOSURE = _flt("MAX_EDGE_EXPOSURE", 0.40)         # 40% in Edge engine
MAX_EXPOSURE_PER_SPORT = _flt("MAX_EXPOSURE_PER_SPORT", 0.25)   # 25% per sport
MAX_EXPOSURE_PER_WINDOW = _flt("MAX_EXPOSURE_PER_WINDOW", 0.35) # 35% in any 4h window
CORRELATION_WINDOW_HOURS = _flt("CORRELATION_WINDOW_HOURS", 4.0)

# ─── LIQUIDITY ───────────────────────────────────────────────────────────────
MIN_MARKET_LIQUIDITY = _flt("MIN_MARKET_LIQUIDITY", 500)
MIN_DAILY_VOLUME = _flt("MIN_DAILY_VOLUME", 5000)

# ─── KELLY ───────────────────────────────────────────────────────────────────
KELLY_FRACTION = _flt("KELLY_FRACTION", 0.25)
MAX_POSITION_PCT = _flt("MAX_POSITION_PCT", 0.10)
MIN_TRADE_SIZE = _flt("MIN_TRADE_SIZE", 5)

# ─── DRAWDOWN GOVERNOR ───────────────────────────────────────────────────────
# Auto-halves Kelly when equity drops this far below peak. Protects compounding.
DRAWDOWN_THRESHOLD = _flt("DRAWDOWN_THRESHOLD", 0.15)       # 15% below peak
DRAWDOWN_KELLY_MULT = _flt("DRAWDOWN_KELLY_MULT", 0.5)      # half-size when triggered

# ─── CIRCUIT BREAKER ─────────────────────────────────────────────────────────
CIRCUIT_DAILY_LOSS_LIMIT = _flt("CIRCUIT_DAILY_LOSS_LIMIT", 0.08)  # pause at 8% daily loss
CIRCUIT_CONSECUTIVE_LOSSES = _int("CIRCUIT_CONSECUTIVE_LOSSES", 5)
CIRCUIT_COOLDOWN_MIN = _int("CIRCUIT_COOLDOWN_MIN", 60)

# ─── HARVEST ─────────────────────────────────────────────────────────────────
HARVEST_ENABLED = _bool("HARVEST_ENABLED", True)
HARVEST_MIN_CONFIDENCE = _flt("HARVEST_MIN_CONFIDENCE", 0.985)
HARVEST_MAX_PRICE = _flt("HARVEST_MAX_PRICE", 0.97)
HARVEST_MIN_PRICE = _flt("HARVEST_MIN_PRICE", 0.80)
HARVEST_MIN_EDGE = _flt("HARVEST_MIN_EDGE", 0.01)
# Partial exit: sell 50% when price reaches this level. Recycles capital.
HARVEST_PARTIAL_EXIT_PRICE = _flt("HARVEST_PARTIAL_EXIT_PRICE", 0.985)
HARVEST_PARTIAL_EXIT_FRAC = _flt("HARVEST_PARTIAL_EXIT_FRAC", 0.50)

# NBA safety filter — research says "20 is the new 12"
# Tighter: 25+ lead AND <6:00 left in Q4
NBA_MIN_LEAD_Q4 = _int("NBA_MIN_LEAD_Q4", 25)
NBA_MAX_CLOCK_Q4_SEC = _int("NBA_MAX_CLOCK_Q4_SEC", 360)

# ─── EDGE FINDER ─────────────────────────────────────────────────────────────
EDGE_ENABLED = _bool("EDGE_ENABLED", True)
EDGE_MIN_EDGE = _flt("EDGE_MIN_EDGE", 0.05)
EDGE_MAX_PRICE = _flt("EDGE_MAX_PRICE", 0.75)
EDGE_MIN_PRICE = _flt("EDGE_MIN_PRICE", 0.25)
EDGE_MIN_HOURS_BEFORE = _flt("EDGE_MIN_HOURS_BEFORE", 2)
EDGE_MAX_HOURS_BEFORE = _flt("EDGE_MAX_HOURS_BEFORE", 48)
EDGE_EXIT_REMAINING = _flt("EDGE_EXIT_REMAINING", 0.02)
EDGE_STOP_LOSS = _flt("EDGE_STOP_LOSS", 0.05)
EDGE_PRE_GAME_EXIT_MIN = _int("EDGE_PRE_GAME_EXIT_MIN", 30)
EDGE_STALE_HOURS = _flt("EDGE_STALE_HOURS", 48)

# ─── ODDS API (odds-api.io — flag-gated) ─────────────────────────────────────
# Free tier: 100 req/hour, 2 bookmakers chosen in user dashboard.
# /odds/multi batches 10 events per request — generous budget.
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "").strip()
ODDS_API_ENABLED = _bool("ODDS_API_ENABLED", False) and bool(ODDS_API_KEY)
ODDS_API_BASE = "https://api.odds-api.io/v3"
# User's selected bookmakers in their odds-api.io dashboard. Free tier = 2.
# Preference order: first match wins. Bet365 and DraftKings are the default.
_bookmaker_csv = os.getenv("ODDS_API_BOOKMAKERS", "Bet365,DraftKings").strip()
ODDS_API_BOOKMAKERS = [b.strip() for b in _bookmaker_csv.split(",") if b.strip()]

# ─── STALE MARKET PENALTY ────────────────────────────────────────────────────
# If Polymarket quote hasn't traded in this long, halve effective confidence.
POLY_STALE_QUOTE_SEC = _int("POLY_STALE_QUOTE_SEC", 1800)
POLY_STALE_PENALTY = _flt("POLY_STALE_PENALTY", 0.5)

# ─── LINEUP WATCHER (v18.1) ──────────────────────────────────────────────────
# Scrapes starting lineups for obscure soccer leagues ~75min before kickoff.
# Used by the Edge engine to boost/dampen effective edge when team news hits.
# Requires api-football.com free tier key (100 req/day). Flag-gated.
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY", "").strip()
API_FOOTBALL_BASE = "https://v3.football.api-sports.io"
LINEUP_WATCHER_ENABLED = _bool("LINEUP_WATCHER_ENABLED", False) and bool(API_FOOTBALL_KEY)

# api-football league IDs → our internal sport keys → (api_league_id, season)
# J2 League (api id 99), A-League (188), Championship (40) at time of writing.
# Season is the starting year for European leagues, calendar year for J-League.
LINEUP_WATCH_LEAGUES = {
    "j2":    (99,  2026),   # J2 League (calendar-year season)
    "aleag": (188, 2025),   # A-League Men 2025-26
    "champ": (40,  2025),   # English Championship 2025-26
}
LINEUP_CHECK_INTERVAL = _int("LINEUP_CHECK_INTERVAL", 120)  # sec between polls
LINEUP_PRE_GAME_WINDOW_MIN = _int("LINEUP_PRE_GAME_WINDOW_MIN", 75)
LINEUP_FETCH_LEAD_MIN = _int("LINEUP_FETCH_LEAD_MIN", 90)

# ─── ESPN SPORTS ─────────────────────────────────────────────────────────────
_S = {"slug": "soccer", "periods": 2, "period_name": "H"}

ESPN_SPORTS = {
    # US Major
    "nba":    {"league": "nba",  "slug": "basketball",  "periods": 4, "period_name": "Q"},
    "wnba":   {"league": "wnba", "slug": "basketball",  "periods": 4, "period_name": "Q"},
    "nhl":    {"league": "nhl",  "slug": "hockey",      "periods": 3, "period_name": "P"},
    "mlb":    {"league": "mlb",  "slug": "baseball",    "periods": 9, "period_name": "Inn"},
    "nfl":    {"league": "nfl",  "slug": "football",    "periods": 4, "period_name": "Q"},
    "ncaab":  {"league": "mens-college-basketball", "slug": "basketball", "periods": 2, "period_name": "H"},
    "ncaaf":  {"league": "college-football", "slug": "football", "periods": 4, "period_name": "Q"},
    # Tier 1 soccer
    "epl":    {"league": "eng.1",  **_S},
    "liga":   {"league": "esp.1",  **_S},
    "seriea": {"league": "ita.1",  **_S},
    "bundes": {"league": "ger.1",  **_S},
    "ligue1": {"league": "fra.1",  **_S},
    "ucl":    {"league": "uefa.champions", **_S},
    "uel":    {"league": "uefa.europa",    **_S},
    # Tier 2
    "mls":    {"league": "usa.1",  **_S},
    "ligamx": {"league": "mex.1",  **_S},
    "erediv": {"league": "ned.1",  **_S},
    "liga2":  {"league": "esp.2",  **_S},
    "lig2fr": {"league": "fra.2",  **_S},
    "bund2":  {"league": "ger.2",  **_S},
    "serieb": {"league": "ita.2",  **_S},
    "porto":  {"league": "por.1",  **_S},
    "scotpr": {"league": "sco.1",  **_S},
    "uecl":   {"league": "uefa.europa.conf", **_S},
    # Tier 3 — Sleeping Lion
    "champ":  {"league": "eng.2",  **_S},
    "jleag":  {"league": "jpn.1",  **_S},
    "j2":     {"league": "jpn.2",  **_S},
    "aleag":  {"league": "aus.1",  **_S},
    "braA":   {"league": "bra.1",  **_S},
    "braB":   {"league": "bra.2",  **_S},
    "kleag":  {"league": "kor.1",  **_S},
    "china":  {"league": "chn.1",  **_S},
    "turk":   {"league": "tur.1",  **_S},
    "norw":   {"league": "nor.1",  **_S},
    "denm":   {"league": "den.1",  **_S},
    "colom":  {"league": "col.1",  **_S},
    "egypt":  {"league": "egy.1",  **_S},
    "libert": {"league": "conmebol.libertadores", **_S},
    "sudam":  {"league": "conmebol.sudamericana", **_S},
    "saudi":  {"league": "sau.1",  **_S},
}

# Sports where ESPN embeds FanDuel/ESPN BET odds
ESPN_ODDS_SPORTS = {"nba", "wnba", "nhl", "mlb", "nfl", "ncaab", "ncaaf", "mls"}

# Map our internal sport key → odds-api.io league slug.
# ALL slugs below VERIFIED against live odds-api.io /events endpoint 2026-04-17.
# Slug format: {country}-{league-kebab}, some with dots/dashes in country name.
_ODDS_API_FULL_MAP = {
    # European top flights
    "epl":    "england-premier-league",
    "liga":   "spain-laliga",
    "seriea": "italy-serie-a",
    "bundes": "germany-bundesliga",
    "ligue1": "france-ligue-1",
    # European second tiers (sleeping lions — often in-season when top flight isn't)
    "champ":  "england-championship",
    "liga2":  "spain-laliga-2",
    "bund2":  "germany-2-bundesliga",
    "lig2fr": "france-ligue-2",
    "erediv2": "netherlands-eerste-divisie",
    # Other in-season European pro men's leagues (from verified list)
    "erediv": "netherlands-eredivisie",
    "ekstra": "poland-ekstraklasa",
    "allsv":  "sweden-allsvenskan",
    "rom":    "romania-superliga",
    "tur":    "turkiye-super-lig",
    "irepr":  "ireland-premier-division",
    "cro":    "croatia-hnl",
    # European cups
    "ucl":    "international-clubs-uefa-champions-league",
    "uel":    "international-clubs-uefa-europa-league",
    # Asia / Oceania
    "china":  "china-chinese-super-league",
    "aleag":  "australia-a-league",
    "india":  "india-indian-super-league",
    "afc":    "international-clubs-afc-champions-league-elite",
    # Off-season (still mapped for when they start)
    "jleag":  "japan-j-league",
    "j2":     "japan-j-league-2",
    "kleag":  "republic-of-korea-k-league-1",
    "braA":   "brazil-serie-a",
    "braB":   "brazil-serie-b",
    # Latin America (in-season ones)
    "colA":   "colombia-primera-a-apertura",
    "libert": "international-clubs-copa-libertadores",
    "sudam":  "international-clubs-copa-sudamericana",
    "ligamx": "mexico-liga-mx",
}

# Default = leagues CONFIRMED to have upcoming games as of 2026-04-17.
# Override with ODDS_API_LEAGUES env var (comma-separated sport keys, or "all").
_DEFAULT_ODDS_LEAGUES = "champ,aleag,erediv2,lig2fr,bund2,seriea,china,tur,allsv,ekstra"
_LEAGUE_FILTER = os.getenv("ODDS_API_LEAGUES", _DEFAULT_ODDS_LEAGUES).strip()
if _LEAGUE_FILTER.lower() == "all":
    ODDS_API_LEAGUE_MAP = _ODDS_API_FULL_MAP
else:
    _wanted = {s.strip() for s in _LEAGUE_FILTER.split(",") if s.strip()}
    ODDS_API_LEAGUE_MAP = {k: v for k, v in _ODDS_API_FULL_MAP.items() if k in _wanted}

# ─── BLOWOUT THRESHOLDS ──────────────────────────────────────────────────────
# NBA tightened to account for pace-and-space era ("20 is the new 12")
WIN_THRESHOLDS = {
    "nba":    [(30,0.60,0.999,"blowout"),(25,0.75,0.998,"blowout"),(20,0.80,0.995,"strong"),(15,0.90,0.992,"strong")],
    "wnba":   [(25,0.60,0.998,"blowout"),(20,0.75,0.995,"blowout"),(15,0.85,0.992,"strong")],
    "nhl":    [(4,0.33,0.998,"blowout"),(3,0.50,0.995,"blowout"),(3,0.66,0.997,"strong"),(2,0.75,0.990,"strong"),(2,0.85,0.993,"safe")],
    "mlb":    [(7,0.55,0.995,"blowout"),(5,0.66,0.990,"blowout"),(4,0.66,0.975,"strong"),(4,0.77,0.985,"strong"),(3,0.77,0.970,"strong"),(3,0.85,0.985,"safe")],
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

# ─── POLYMARKET SERIES IDs ───────────────────────────────────────────────────
# Verified against live /sports endpoint 2026-04-17.
POLY_SERIES_IDS = {
    # US major
    "nba":    "10345",
    "wnba":   "10105",
    "nfl":    "10187",
    "nhl":    "10346",
    "mlb":    "3",
    "ncaab":  "39",
    "ncaaf":  "10210",
    "mls":    "10189",
    # European top flights
    "epl":    "10188",
    "liga":   "10193",
    "seriea": "10287",
    "bundes": "10194",
    "ligue1": "10195",
    "erediv": "10286",
    "porto":  "10330",
    # European second tiers (sleeping lion)
    "champ":  "10355",   # English Championship
    "liga2":  "10672",
    "bund2":  "10670",
    "lig2fr": "10675",
    # European cups
    "ucl":    "10204",
    "uel":    "10209",
    # Asia / Oceania
    "jleag":  "10360",   # J1
    "j2":     "10443",   # J2 — prime target
    "kleag":  "10444",
    "china":  "10439",
    "aleag":  "10438",
    # Latin America
    "libert": "10289",
    "sudam":  "10291",
    "braA":   "10359",
    "braB":   "10973",
    "ligamx": "10290",
    "argA":   "10285",
    "col":    "10437",
    # Europe fringe
    "turk":   "10292",
    "norw":   "10362",
    "denm":   "10363",
    "saudi":  "10361",
}

POLY_GAMES_TAG_ID = "100639"

# ─── SPORT KELLY MULTIPLIERS (Harvest risk tier) ─────────────────────────────
SPORT_RISK_MULTIPLIER = {
    "nhl": 1.0, "ncaaf": 1.0,
    **{s: 1.0 for s in SOCCER_SPORTS},
    "nfl": 0.8, "mlb": 0.8,
    "nba": 0.5, "ncaab": 0.5, "wnba": 0.5,
}

# ─── SLEEPING LION (Edge multiplier for stale-pricing leagues) ───────────────
SLEEPING_LION = {
    "epl": 1.0, "liga": 1.0, "seriea": 1.0, "bundes": 1.0, "ligue1": 1.0,
    "ucl": 1.0, "uel": 1.0,
    "nba": 1.0, "nfl": 1.0, "mlb": 1.0, "nhl": 1.0,
    "ncaab": 1.0, "ncaaf": 1.0, "wnba": 1.0,
    "mls": 1.2, "ligamx": 1.2, "erediv": 1.2, "porto": 1.2,
    "liga2": 1.2, "lig2fr": 1.2, "bund2": 1.2, "serieb": 1.2,
    "scotpr": 1.2, "uecl": 1.2,
    "champ": 1.5, "jleag": 1.5, "j2": 1.5, "aleag": 1.5,
    "braA": 1.5, "braB": 1.5, "kleag": 1.5, "china": 1.5,
    "norw": 1.5, "denm": 1.5, "colom": 1.5, "egypt": 1.5,
    "turk": 1.3, "libert": 1.3, "sudam": 1.3, "saudi": 1.3,
}

# ─── EDGE SIZE LADDER ────────────────────────────────────────────────────────
EDGE_SIZE_LADDER = [
    (0.15, 2.0),
    (0.10, 1.5),
    (0.07, 1.2),
    (0.05, 1.0),
]

# ─── FUTURES BLOCK ───────────────────────────────────────────────────────────
FUTURES_BLOCK = {
    "champion", "mvp", "winner", "playoff", "series", "division",
    "conference", "prop", "total points", "over/under", "spread",
    "most", "season", "award", "draft", "futures", "rookie", "all-star",
    "leader", "scoring", "outright", "relegation", "relegated",
    "golden boot", "ballon", "transfer", "finish", "place",
    "top 4", "top 6", "top half", "bottom", "promoted", "promotion",
    "survive", "survival", "which club", "which team", "who will",
    "make playoffs", "win total", "over under", "regular season",
    "1st half", "first half", "halftime", "both teams to score", "btts",
    "clean sheet", "next goal", "correct score",
}

# ─── LIMITS FOR DASHBOARD ────────────────────────────────────────────────────
SCAN_LOG_MAX = _int("SCAN_LOG_MAX", 400)
BLOWOUT_LOG_MAX = _int("BLOWOUT_LOG_MAX", 200)
EDGES_LOG_MAX = _int("EDGES_LOG_MAX", 300)
EQUITY_CURVE_MAX = _int("EQUITY_CURVE_MAX", 5000)
TRADE_HISTORY_MAX = _int("TRADE_HISTORY_MAX", 1000)

# ─── DASHBOARD CORS ──────────────────────────────────────────────────────────
CORS_ORIGINS = os.getenv(
    "CORS_ORIGINS",
    "https://signal-sports-dashboard.vercel.app,http://localhost:5173",
).split(",")
