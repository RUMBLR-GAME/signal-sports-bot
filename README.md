# Signal Harvest v18 — Bot

Polymarket sports trading bot. Two engines, paper mode by default, designed to compound from $1,000.

## What's new in v18 (vs v17)

### Economic fixes (these alone change whether the bot is profitable)

| # | Bug in v17 | Fix in v18 | File |
|---|---|---|---|
| 1 | `MAX_TOTAL_EXPOSURE × STARTING_BANKROLL` locked deployed cap at $600 forever — bot **could not compound** | Cap now scales with current equity. $5K equity → $3K cap. | `sizing.py` |
| 2 | `equity = STARTING_BANKROLL - open_cost + total_pnl` — wrong, didn't mark to market, showed depressed numbers | Proper mark-to-market: `equity = cash + Σ(shares × current_price)` | `positions.py` |
| 3 | `resolve_position` checked `winner == p.side` but `p.side` was `"YES"` for harvest, outcome name for edge — every Edge win mis-resolved | Dedicated `bet_outcome` field, fuzzy-match resolution | `positions.py` |
| 4 | `"Milan"` substring-matched Inter Milan; `"MIL"` matched inside "Milan" | Word-boundary matching for short terms, ambiguity rules for tokens like milan/wolves/wanderers. **17/17 test cases pass** | `teams.py` |
| 5 | Sub-Kelly bets forced up to $5 minimum — broke Kelly discipline | Below $5 = skip | `sizing.py` |

### New capabilities

- **Polymarket Sports WebSocket** as primary live feed (~100ms vs ~30s REST polling). ESPN is now the fallback. Biggest latency win in the bot.
- **Polymarket Market WebSocket** maintains local orderbook per token — live bid/ask pushed instead of polled.
- **Drawdown governor** — Kelly halves when equity drops 15% from peak. Protects compounding after a bad run.
- **Circuit breaker** — auto-pause on 5 consecutive losses OR 8% daily drawdown, 60-min cooldown.
- **Correlation caps** — 25% max per sport, 35% per 4h time window. Prevents "one bad NBA night kills half the bankroll."
- **Harvest partial exits** at 0.985 (sell 50%) — recycles capital faster. Research shows holding to resolution leaves money on the table.
- **Stale market penalty** — Polymarket quote that hasn't traded in 30 min has its effective edge halved.
- **The Odds API integration** (flag-gated) — unlocks soccer edge finding when subscribed. Sharp-book preference (Pinnacle > Betfair > Bet365).
- **Series-ID verification on startup** — cross-checks `POLY_SERIES_IDS` against live `/sports` endpoint, flags mismatches in the dashboard.
- **Kill switch endpoints** — `POST /api/pause`, `POST /api/resume`, `POST /api/reset`.
- **Per-sport P&L attribution** in the API snapshot.
- **Futures block expanded** to filter first-half, BTTS, correct-score, etc.

### Performance fixes

- Single shared `aiohttp.ClientSession` across every fetcher (was: new session per call)
- `asyncio.to_thread` wraps all sync CLOB calls (was: blocking the event loop)
- Single `espn.fetch_all()` returns blowouts + live + odds in one pass (was: two full scans)
- `MarketWS` replaces per-scan `get_price()` REST polls

### Explicitly removed

- **Poly Arber engine** deleted. Research showed arb duration is down to 2.7 seconds on Polymarket, captured by sub-100ms bots. From Brisbane over REST you will never compete. Not your edge.

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `PAPER_MODE` | `true` | `false` to trade real USDC |
| `STARTING_BANKROLL` | `1000` | Initial equity. Changed value requires `FORCE_RESET=true` one-shot |
| `FORCE_RESET` | `false` | One-time state wipe: set `true`, redeploy, set back to `false` |
| `PORT` | `8080` | Injected by Railway |
| `REDIS_URL` | empty | Optional Redis for state |
| `LOG_LEVEL` | `INFO` | |
| `HARVEST_ENABLED` | `true` | Toggle engine 1 |
| `EDGE_ENABLED` | `true` | Toggle engine 2 |
| `ODDS_API_KEY` | empty | Set when you subscribe to the-odds-api.com |
| `ODDS_API_ENABLED` | `false` | Set `true` after setting the key |
| `POLYMARKET_PRIVATE_KEY` | empty | Required for live trading |
| `POLYMARKET_FUNDER_ADDRESS` | empty | Required for live trading |
| `SIGNATURE_TYPE` | `1` | `1` = POLY_PROXY |
| `DRAWDOWN_THRESHOLD` | `0.15` | Auto-halve Kelly at this drawdown |
| `CIRCUIT_DAILY_LOSS_LIMIT` | `0.08` | Auto-pause if daily loss exceeds |
| `MAX_EXPOSURE_PER_SPORT` | `0.25` | Correlation cap |
| `MAX_EXPOSURE_PER_WINDOW` | `0.35` | Correlation cap for games within 4h |
| `HARVEST_PARTIAL_EXIT_PRICE` | `0.985` | Price at which to sell 50% |

## Deploy

```bash
# From your Mac, signal-sports-bot repo:
cd ~/signal-sports-bot
# Copy all the v18 files over
cp ~/Downloads/v18/bot/*.py .
cp ~/Downloads/v18/bot/{Procfile,railway.json,requirements.txt,.gitignore} .
# Commit
git add -A
git commit -m "v18: compounding fix, WS integration, drawdown governor, partial exits"
git push origin main
# Railway auto-deploys in ~60s

# Verify
curl -s https://web-production-72709.up.railway.app/health
curl -s https://web-production-72709.up.railway.app/api/state | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(f'Equity: \${d[\"equity\"]} | Cash: \${d[\"cash\"]} | Deployed: \${d[\"open_cost\"]}')
print(f'Peak: \${d[\"peak_equity\"]} | DD: {d[\"drawdown_pct\"]*100:.1f}%')
print(f'WS Sports: {d[\"ws_sports_connected\"]} | WS Market: {d[\"ws_market_connected\"]}')
print(f'Series IDs verified: {sum(1 for v in d[\"series_verified\"].values() if v)}/{len(d[\"series_verified\"])}')
"
```

## Architecture

```
main.py               # orchestrator + scan timing
├── polymarket_ws.py  # Sports WS + Market WS (NEW — primary data source)
├── espn.py           # fallback + pre-game odds (US sports)
├── odds_api.py       # soccer odds via the-odds-api.com (NEW, flag-gated)
├── clob.py           # Polymarket Gamma + CLOB (async wrappers)
├── harvest.py        # engine 1: blowout detection
├── edge.py           # engine 2: pre-game convergence
├── positions.py      # state + circuit breaker + correct equity math
├── sizing.py         # Kelly + drawdown governor + correlation caps
├── teams.py          # fuzzy matcher (17/17 tests)
├── api.py            # REST: state, pause, resume, reset
└── config.py         # all parameters in one place
```

## Testing

Run the teams matcher:
```bash
python3 -c "from teams import match_game_to_market as m; print(m('AC Milan','MIL','Inter Milan','INT','Derby',['AC Milan','Inter Milan']))"
# Expected: (0, 1)
```

Smoke test sizing:
```bash
python3 -c "
from sizing import compute_bet_size
class FakePM:
    open_cost = 0.0
    peak_equity = 5000.0
    def deployed_by_engine(self, e): return 0
    def deployed_by_sport(self, s): return 0
    def deployed_in_window(self, ts): return 0
print(compute_bet_size('harvest', 0.9, 0.99, 5000, FakePM(), sport='mlb'))
# Expected: size ~\$400 (vs v17's capped \$80)
"
```

## Known caveats

1. **Series IDs are my best extrapolation for some newer leagues.** The bot verifies against `/sports` on startup and flags mismatches in the dashboard. Check those flags on first deploy.
2. **Sports WS message schema** isn't fully documented by Polymarket — my normalizer is best-effort. If you see the `live_games` list filling only from ESPN (WS not contributing), that's why. It's not a crash — ESPN remains the working fallback.
3. **First live run** after switching from v17: state schema changed. Set `FORCE_RESET=true` on the first deploy, let it restart, then set back to `false`.
4. **Brisbane latency**: you're ~250ms from Polymarket's London backend. You are not going to win sub-second arb. Your edge is disciplined compounding on 2-48h pre-game markets and mid-game blowouts where a 2-5 second edge is still tradable.
