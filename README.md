# Trading Agent

Trading Agent is a local-first intraday workbench for NSE equities. It ingests Kite Connect candles and RSS news, scores every watchlist symbol with rule, ML and LLM paths, sizes trades against portfolio and intraday risk limits, routes orders through one guarded router, and reports performance net of Zerodha charges. It runs as a FastAPI backend with a React + Material UI dashboard.

> **Paper mode is the default.** Live Zerodha orders need two explicit settings (see [Risk controls and execution](#risk-controls-and-execution)) and have not been verified against a real account. No strategy edge has been demonstrated. This is not financial advice.

Project status, open work and decision history: [MEMORY.md](MEMORY.md).

## Dashboard

| Tab | What it is for |
|---|---|
| **Dashboard** | Classic view: watchlist sidebar with bias and contradictions, bullish/bearish Top Opportunity, symbol detail (chart, patterns, technical summary with floor pivots, sizing, news, rule/LLM/ML signals), wallet, position monitor, morning digest |
| **Research** | Find → Inspect → Validate → Paper review workflow: full-watchlist scan, Plotly chart, walk-forward optimizer, trade-outcome validation, BUY/SELL outcome models |
| **Paper operations** | Persistent paper book: open, monitor and close positions |
| **Risk & execution** | Kill switch, square-off, session status, router orders, reconciliation, cost calculator, audit trail |
| **Analytics** | Net-of-cost performance and attribution, volume board (RVOL, VWAP, liquidity, volume forecast), cross-sectional ranking research, ML drift monitor |
| **Signal Pipeline** | Latest rule/LLM/ML decision per symbol |
| **Transactions** | Transaction history and strategy status |
| **News** | Headlines matched to the watchlist, with cached sentiment |

All tabs read the same research scan snapshot, so rankings, opportunities and sizing agree. Refreshing a page never starts a scan, an LLM call or an order.

## What works without broker credentials

The API, dashboard, tests, stored-data analysis, backtests and paper trading run without Kite credentials. Credentials and a current daily access token are needed only to download or stream broker data, or to place live orders.

## Quick start: local

Use Python 3.11 or 3.12 and Node 22. Copy `.env.example` only on first setup.

```powershell
# Windows PowerShell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
Copy-Item .env.example .env
python -m trading_agent.scripts.validate_setup --skip-llm   # pre-flight check
trading-agent-api                                           # creates the SQLite schema
```

```bash
# macOS/Linux
python3 -m venv .venv && source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"
cp .env.example .env
python -m trading_agent.scripts.validate_setup --skip-llm
trading-agent-api
```

Dashboard, in a second terminal:

```bash
cd frontend
npm ci
npm run dev        # PowerShell: npm.cmd run dev
```

Open http://localhost:5173 (dashboard), http://localhost:8000/docs (API) and http://localhost:8000/api/v1/health.

## Quick start: Docker

Compose uses the same SQLite/Parquet storage as local runs. `./data` and `./models` are mounted into every Python service; ports bind to loopback only.

```bash
cp .env.example .env
docker compose --profile live build
docker compose up -d api frontend
docker compose ps
```

### Daily operating schedule

| When | Command | Purpose |
|---|---|---|
| Before the first market-data operation each day | `docker compose run --rm tools trading-agent login` | Refresh the Kite token in `data/.access_token` |
| After the close | `docker compose run --rm tools trading-agent data-check --universe all` | Confirm every stored symbol is complete through today; it prints the backfill command for any gaps (see [Market data and backfill](#market-data-and-backfill)) |
| After downtime | `docker compose run --rm tools trading-agent heal-gaps --interval 5minute --limit 100` | Replay a one-day overlap and catch up; drop `--limit` after reviewing |
| Market session | `docker compose --profile live up -d api frontend ticker scheduler` | Live candles, scheduled decisions, 15:15 square-off, 15:40 drift check |
| After startup | `docker compose ps` | Confirm services are healthy |
| Troubleshooting | `docker compose logs -f --tail=200 api ticker scheduler` | Stream logs; `Ctrl+C` leaves the view only |
| End of session | `docker compose down` | Stop containers; data and token are kept |
| After code or dependency changes | `docker compose --profile live build`, then the `up -d` command again | Rebuilding alone does not replace running containers |

Use the same interval as `STORAGE_CANDLE_INTERVAL` (default `5minute`) for gap healing; minute and five-minute data are stored separately. Exit code 1 from `heal-gaps` means failure: read the `errors` summary before retrying, and run one download at a time.

## Kite setup

1. Set `KITE_API_KEY`, `KITE_API_SECRET`, `KITE_USER_ID`, `KITE_PASSWORD`, `KITE_TOTP_SECRET` and the redirect URL registered with Kite in `.env`.
2. Once: `playwright install chromium`.
3. Each trading day: `trading-agent login` (automated) or `python manual_login.py` (manual fallback).

The token is stored at `data/.access_token`, ignored by Git and shared with Docker.

## Market data and backfill

Candles are stored as Parquet under `STORAGE_PARQUET_ROOT` (default `data/parquet`), one folder per symbol and interval. `minute` and `5minute` are stored separately; the live ticker writes `STORAGE_CANDLE_INTERVAL`, and research and the ranker trial read `minute`. Every download needs a valid Kite token (`trading-agent login`).

| Command | Use it for |
|---|---|
| `trading-agent ingest SYMBOL... --interval minute --days 100` | The last N days for a few symbols |
| `trading-agent backfill SYMBOL... --interval minute --years 2` | A long history: the full range, chunked to Kite's per-request limits (minute: 7 days; 5minute: 30; 15/30minute: 200) |
| `trading-agent backfill --universe all --interval minute --days 30 --missing-only` | Top-up: request only sessions that are missing or incomplete |
| `trading-agent heal-gaps --interval 5minute` | Catch up the live interval after the ticker or the machine was down |
| `trading-agent data-check --universe all` | Check that everything is current; exit 1 if not |

**`--universe`** (for `backfill` and `data-check`) picks the symbols when none are given:

- `watchlist`: `symbols.txt`, the default;
- `nifty500`: `symbols_nifty500_plus.txt`, the ranker trial's universe;
- `all`: every symbol already stored for the interval, about 2,300 NSE stocks.

**`--missing-only`** checks each symbol against the trading calendar seen across the whole store. It fetches:

- sessions with no bars;
- partial sessions, such as a morning the live ticker recorded before it stopped;
- weekdays after the newest stored session.

Weekdays that no symbol has (holidays) are skipped. A session counts as complete with the 09:15 bar and a bar at 15:14 or later: since NSE's closing auction (3 August 2026) about 40% of stocks have no continuous-trading bars after 15:14. Thinly traded stocks often have no trade at 09:15 or near 15:14, so their bars never look complete. Every successful fetch therefore records the window it covered in `data/ingest_coverage/<interval>/<SYMBOL>.json`, and a session that was fully fetched counts as covered even with sparse or no bars. Thin stocks are not re-requested on every run. Without `--missing-only`, `backfill` re-requests the whole range; overlapping candles replace stored ones by timestamp, so nothing is duplicated.

**How long it takes.** Requests are paced at about 2.5 a second.

| Job | Approximate time |
|---|---|
| One year of minute data, one symbol (52 requests) | ~20 s |
| One year for the Nifty 500 from empty | ~3 hours |
| 30-day top-up of all ~2,300 symbols | 30–40 minutes, plus ~5 minutes to scan the store |
| A `data-check --universe all` | ~5 minutes; it reads the store only, except for one token check |

Rerunning after an interruption continues from what was stored. Run one download at a time.

**What `data-check` reports:**

- **The store:** its newest session against the last weekday whose market has closed.
- **Each symbol** (last 30 days by default, `--days N`):
  - up to date;
  - behind: last complete session older than the newest;
  - holes: sessions missing inside the window;
  - empty: no data at all.
- **Symbols Kite no longer lists** (renamed, delisted, REITs) are shown separately and never fail the check.
- **Also:** the Kite token, the ticker heartbeat and each trial arm.

It ends with the exact `backfill` command that fills the gaps.

```bash
trading-agent data-check --universe all            # e.g. "up to date 494 | behind 1794 | ... not listed by Kite 24"
trading-agent backfill --universe all --interval minute --days 30 --missing-only
trading-agent data-check --universe all            # confirm; exit 0 when current
```

In Docker, prefix any of these with `docker compose run --rm tools`.

**Pitfalls:**

- **Relative storage path:** `STORAGE_PARQUET_ROOT=data/parquet` is relative to the working directory. A job started from a linked worktree writes its own empty store. Run data jobs from the main checkout, or set an absolute path.
- **Renamed or delisted symbols:** symbols not in Kite's instrument list fail with `Symbol not found`. Nothing can be fetched for them; remove them from the symbol files, or ignore them.
- **5-minute data:** heal it with `--interval 5minute`. Minute data does not fill it.

## Common workflow

```bash
# Watchlist and data
python -m trading_agent.ingest.build_nifty500_symbols          # resolves instrument tokens; --no-resolve to skip
trading-agent ingest RELIANCE TCS INFY --days 100
trading-agent backfill RELIANCE --years 2                       # chunked, resumable, rate limited
trading-agent backfill --interval minute --days 365 --missing-only  # only sessions missing or incomplete
trading-agent data-check --universe all                         # is every stored symbol current? exit 1 if not
trading-agent backfill --universe all --interval minute --days 30 --missing-only  # fill what data-check found
trading-agent storage-stats

# Research
trading-agent diagnose RELIANCE TCS
trading-agent backtest RELIANCE TCS --start 2025-01-01 --end 2025-06-30
python -m trading_agent.research.walk_forward_optimizer --selftest   # vectorized vs loop reference
python -m trading_agent.research.walk_forward_optimizer RELIANCE --interval minute --cost-bps 5
python -m trading_agent.research.walk_forward_optimizer --all        # whole watchlist, expensive

# Nifty 50 index candles (market-regime context for Strategy Lab; never traded)
trading-agent ingest NIFTY50 --interval minute --days 60
trading-agent optimize strategy-lab RELIANCE --interval minute   # includes opening-range breakout profiles

# Cross-sectional ranking research (walk-forward, after costs, vs random picks)
trading-agent optimize cross-sectional --interval minute             # --max-lag 240 for the paper's lags

# Ranking lab: 60 pre-registered cross-sectional variants, selection frozen on validation
trading-agent optimize ranking-lab --snapshots data/research/ranking_lab/snapshots.parquet

# Score stored rule/ML/LLM signals on realised returns (scheduler runs it at 15:55)
trading-agent optimize decision-outcomes --days 30 --horizon 60

# Prospective paper trial of the frozen ranker (scheduler runs pick 09:16 and settle 15:50)
trading-agent optimize ranker-trial start --arm rf                 # freezes config, universe, first session
trading-agent optimize ranker-trial start --arm hgb                # second arm: gradient boosting
trading-agent optimize ranker-trial status                          # every started arm

# Strategy search: frozen selection on development/validation, baselines, walk-forward, then holdout
trading-agent optimize strategy-search                              # --no-ml to skip ML; ~20-30 min

# ML drift monitoring
python -m trading_agent.strategy.model_monitoring baseline      # once, before drift reports mean anything
python -m trading_agent.strategy.model_monitoring report

# Market hours (local)
trading-agent-ticker
trading-agent-scheduler
```

`trading-agent --help` and `trading-agent COMMAND --help` list every option. Prefix any command with `docker compose run --rm tools` to run it in Docker.

## Decision paths

- **Rule agent**: the validated path, and the only one that sizes trades by default.
- **ML and LLM agents**: run in parallel and write to their own tables (`ml_signals`, `llm_signals`). They never override the rule signal. Rules + ML mode holds when the paths disagree or when ML is unavailable or withdrawn for drift.
- **LLM**: OpenRouter only (`LLM_PROVIDER=openrouter`). Calls are spaced by `LLM_MIN_INTERVAL_SECONDS` and retried on 429/5xx, honouring `Retry-After`. The code default is `mock`, which makes no calls.
- **Evidence gate**: with **Require qualified outcome evidence** on (the default), a scan only proposes plans backed by passing trade-outcome validation. Missing or expired evidence gives no plan, never an invented probability. See [USER_GUIDE.md](USER_GUIDE.md#trade-outcome-validation-and-win-estimates).
- **Outcome models**: `python -m trading_agent.strategy.outcome_cli status|train|paper|promote`. The first candidate failed qualification and nothing is promoted. See [USER_GUIDE.md](USER_GUIDE.md#buysell-outcome-models).

## Risk controls and execution

Every entry, whether from the dashboard, the paper book or the API, passes the same checks before any order is sent:

- Kill switch (persists across restarts; an unreadable state file blocks entries).
- Session: no entries before 09:15, after `PAPER_ENTRY_CUTOFF` (15:00) or on weekends.
- Daily trade cap (`PAPER_MAX_TRADES_PER_DAY`), and optionally one entry per symbol per day (`PAPER_ONE_ENTRY_PER_SYMBOL_PER_DAY`).
- Portfolio exposure, stop risk, position count, single-name cap, realized daily loss and drawdown.
- Participation caps: no position above `EXECUTION_MAX_PARTICIPATION_PCT` of the stock's average daily traded value, or above `EXECUTION_MAX_REMAINING_VOLUME_PCT` of the volume still forecast to trade today.

The scheduler squares off every open position at `PAPER_SQUARE_OFF_TIME` (15:15). Each entry, exit, fill, rejection and halt is written to `data/controls/audit-YYYY-MM-DD.jsonl`.

Paper fills move `EXECUTION_SLIPPAGE_BPS` (5 bps) against you. Net P&L subtracts modelled Zerodha intraday charges: brokerage, STT, exchange, SEBI, stamp duty and GST.

Live mode sends Kite MARKET orders with product MIS and needs **both**:

```dotenv
EXECUTION_MODE=live
EXECUTION_LIVE_CONFIRMATION=I-ACCEPT-REAL-MONEY-RISK
```

Do a small supervised round trip and check reconciliation before relying on it.

## Configuration

Settings come from environment variables, then `.env`, then code defaults. Full list: [.env.example](.env.example).

| Variable | Default | Purpose |
|---|---|---|
| `DB_URL` | `sqlite:///data/trading.db` | SQLite database (WAL mode) |
| `DATA_DIR` / `STORAGE_PARQUET_ROOT` | `data` / `data/parquet` | Runtime data and candles |
| `ML_MODEL_DIR` | `models` | Model artifacts and drift baseline |
| `STORAGE_CANDLE_INTERVAL` | `5minute` | Ticker, scheduler and monitor interval |
| `API_HOST` / `API_PORT` / `API_WORKERS` | `127.0.0.1` / `8000` / `1` | Keep one worker |
| `API_AUTH_TOKEN` | empty | Optional bearer token; set before sharing access |
| `PAPER_INITIAL_CAPITAL` | `1000000` | Keep fixed for an existing ledger |
| `PAPER_ENTRIES_ENABLED` | `true` | `false` blocks new entries after restart; exits stay available |
| `PAPER_MAX_POSITIONS` | `10` | Open positions |
| `PAPER_MAX_EXPOSURE_PCT` / `PAPER_MAX_RISK_PCT` | `0.50` / `0.06` | Aggregate notional and stop risk |
| `PAPER_MAX_TRADE_RISK_PCT` / `PAPER_MAX_SINGLE_POSITION_PCT` | `0.02` / `0.10` | Per-trade risk and per-symbol notional |
| `PAPER_MAX_DAILY_LOSS_PCT` / `PAPER_MAX_DRAWDOWN_PCT` | `0.03` / `0.15` | Realized loss limits |
| `PAPER_MAX_TRADES_PER_DAY` | `20` | Daily entry cap |
| `PAPER_ENTRY_CUTOFF` / `PAPER_SQUARE_OFF_TIME` | `15:00` / `15:15` | IST session discipline |
| `PAPER_ENFORCE_SESSION` | `true` | `false` disables time gates (tests); the trade cap still applies |
| `PAPER_ONE_ENTRY_PER_SYMBOL_PER_DAY` | `false` | `true` blocks re-entering a symbol already traded today |
| `EXECUTION_MODE` / `EXECUTION_LIVE_CONFIRMATION` | `paper` / empty | Live orders need both set |
| `EXECUTION_SLIPPAGE_BPS` / `EXECUTION_PRODUCT` | `5` / `MIS` | Paper slippage; live product |
| `EXECUTION_MAX_PARTICIPATION_PCT` | `0.01` | Share of average daily traded value |
| `EXECUTION_MAX_REMAINING_VOLUME_PCT` | `0.05` | Share of the volume still forecast to trade today |
| `LLM_PROVIDER` | `mock` in code, `openrouter` in `.env.example` | `mock` makes no calls |
| `LLM_API_KEY` / `OPENROUTER_API_KEY` / `LLM_MODEL` | empty / empty / `gpt-4o-mini` in code (`.env.example`: `google/gemini-2.0-flash-001`) | OpenRouter credentials and model id |
| `LLM_QUICK_MODEL` / `LLM_DEEP_MODEL` | empty (use `LLM_MODEL`) | Cheap model for headline sentiment and setup checks; stronger model for LLM decisions |
| `LLM_MIN_INTERVAL_SECONDS` / `LLM_MAX_RETRIES` | `4.5` / `4` | Throttling and 429 retries |
| `MONITOR_PSI_WARN` / `MONITOR_PSI_DRIFT` | `0.1` / `0.25` | Drift thresholds per feature |
| `MONITOR_DRIFT_FEATURES_TO_FALLBACK` | `3` | Drifted features that withdraw ML |

The dashboard shows an access-token form only when `API_AUTH_TOKEN` is set. Tokens stay in browser memory. Remote access additionally needs TLS and an authenticated gateway.

## Architecture

```text
Kite REST/WebSocket, RSS -> ingest -> Parquet candles + SQLite (WAL)
                                  |
                core indicators, patterns, signals
                                  |
          strategy: rule (authoritative) | ML | LLM (parallel)
                                  |
     analysis: research scan, opportunities, volume, performance
                                  |
  risk controls -> execution router -> paper fill | Kite order
                                  |
             FastAPI (/api, /api/v1) -> React + MUI
```

```text
src/trading_agent/
  api/        FastAPI app; routers incl. legacy_ui (/api) and operations (/api/v1)
  analysis/   research scan, opportunities, technical summary, volume, performance
  core/       config, indicators, patterns, signals, LLM client, freshness
  execution/  order router, Zerodha costs, reconciliation
  ingest/     Kite auth/history/live ticker, instruments, news, watchlists
  research/   backtests, diagnostics, walk-forward optimizer
  risk/       ledger guard, portfolio limits, controls, position monitor
  scripts/    scheduler, validate_setup, supervisor, maintenance
  storage/    SQLAlchemy models, Parquet store
  strategy/   candidates, ML/LLM agents, outcome models, drift monitoring
frontend/     React + TypeScript + Vite + MUI
migrations/   Alembic revisions 001-003
tests/        unit, integration and browser smoke tests
```

## Database lifecycle

The API creates missing tables on startup. Migration-controlled deployments run:

```bash
alembic upgrade head       # Docker: docker compose run --rm tools alembic upgrade head
alembic current
```

Revision 003 adds the `orders` table. Back up an existing database with the SQLite backup API and stop writers before upgrading. Never edit an applied migration; add a new revision. See [USER_GUIDE.md](USER_GUIDE.md#database-upgrades).

## Development checks

```bash
PYTHONPATH=src python -m pytest -q -p no:cacheprovider --ignore=tests/docker_smoke.py --ignore=tests/dashboard_auth_smoke.py --ignore=tests/dashboard_workflow_smoke.py
ruff check src migrations
cd frontend && npm run typecheck && npm run build && cd ..
python tests/dashboard_workflow_smoke.py
python tests/dashboard_auth_smoke.py
```

`PYTHONPATH=src` avoids importing a different installed copy of the package. CI runs on `main`, `develop`, `gemini`, `rework` and `unified-platform`. Evidence and gates: [READINESS.md](READINESS.md).

## Linked worktree layout

If the main checkout is on another branch and this branch is in `.worktrees/gemini-repair`, build from the worktree and keep the main folder's `.env`, `data` and `models`:

```powershell
$repo = (Get-Location).Path   # main trading-agent folder
$wt = Join-Path $repo '.worktrees/gemini-repair'
docker build -t trading-agent-app:latest $wt
docker build -t trading-agent-frontend:latest "$wt/frontend"
docker compose --project-directory $repo -f "$wt/docker-compose.yml" up -d --no-build api frontend
docker compose --project-directory $repo -f "$wt/docker-compose.yml" --profile live up -d --no-build ticker scheduler
```

Keep separate databases per branch until schemas are checked; `data/trading_agent.db` (old) and `data/trading.db` (current) have different contracts.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `trading-agent` not found | Activate the virtual environment and `pip install -e .`, or use `python -m trading_agent.cli` |
| Tests import stale code | Run with `PYTHONPATH=src` |
| `No data available` | Log in, `trading-agent ingest SYMBOL`, then `trading-agent storage-stats` |
| Kite token rejected | `trading-agent login` again |
| Playwright browser missing | `playwright install chromium`, or `python manual_login.py` |
| Entry rejected with 409 | Read the reason: kill switch, after 15:00, weekend, trade cap, duplicate symbol, exposure, risk, daily loss, drawdown or participation |
| Entry rejected with 502 | The broker rejected the order; the reservation was removed. Check the audit log |
| Sentiment calls return 429 | Raise `LLM_MIN_INTERVAL_SECONDS`; retries already honour `Retry-After` |
| Drift monitor says "no baseline" | Run `python -m trading_agent.strategy.model_monitoring baseline` |
| ML shows as withdrawn | Drift fallback is active; review **Analytics → ML drift monitor**, then **Restore ML** |
| SQLite is locked | Stop duplicate ticker/scheduler processes |
| `no such column` / missing `orders` table | Back up, stop writers, `alembic upgrade head` |
| Rebuilt code appears unchanged | Rerun `docker compose up -d ...`; building does not replace containers |
| API returns 401 | Enter the configured bearer token in the dashboard |

## Documentation map

- [MEMORY.md](MEMORY.md): **project status, feature map, open work and decision log**
- [USER_GUIDE.md](USER_GUIDE.md): detailed operation and recovery
- [READINESS.md](READINESS.md): verification commands, evidence and release gates
- [ARCHITECTURE.md](ARCHITECTURE.md): binding decisions
- [DESIGN.md](DESIGN.md): components and data flow
- [PRD.md](PRD.md): product scope
- [RULES.md](RULES.md): contributor rules
- [STRATEGIES.md](STRATEGIES.md), [EDGE_ANALYSIS.md](EDGE_ANALYSIS.md): historical strategy research

## License

No license file is included. Treat the repository as all-rights-reserved unless the owner adds one.
