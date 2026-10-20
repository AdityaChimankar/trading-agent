# Trading Agent

Trading Agent is a local-first research and paper-trading system for NSE intraday data. It can ingest Kite Connect candles, store time-series data in Parquet, evaluate rule/ML/LLM decision paths, backtest strategies, monitor paper positions, and expose the results through a FastAPI API and React dashboard.

> This project does not place broker orders. It is research and paper-trading software, not financial advice.

The supported interface is the restored research dashboard: full-watchlist hybrid rankings, bullish/bearish opportunities, Plotly charts, news sentiment, and risk-based plans. The **Paper operations** tab retains the persistent paper book. This is **not an institutional live-trading platform**. See [READINESS.md](READINESS.md) for limitations and release gates.

## Unified dashboard workflow

One symbol selection and shared settings connect the workspace:

1. **Find opportunities** — explicitly scan the configured universe; inspect progress and completed bullish/bearish plans.
2. **Inspect selected stock** — selecting a top pick opens its Plotly chart, rule/ML decision, scan-based stop/target/size and cached news.
3. **Validate strategy** — run the Walk-Forward Optimizer, check fixed-rule trade outcomes, and inspect BUY/SELL model qualification. Results remain tied to their exact inputs while navigating this page session.
4. **Paper review** — the selected symbol and interval carry into the persistent paper workspace. Entries remain manual; changing symbol clears unsubmitted price/quantity fields.

Walk-forward tuning does **not** automatically apply parameters, qualify a scan or promote ML. Rule and promoted-ML policy buttons explicitly update shared settings for your review; you must still start a fresh scan. New validation evidence, refreshed news or paper-book changes invalidate existing scan plans in this workspace. Refreshing views or changing steps never starts training, scanning, news-provider calls or orders.

After installing this UI update in Docker, run `docker compose build api frontend`, then `docker compose up -d api frontend`, and reload your browser. No image pruning or data reset is needed. Local frontend developers can use `npm.cmd run build` from `frontend` on Windows (or `npm run build` on other shells). See [USER_GUIDE.md](USER_GUIDE.md#research-panels) for the coordinated workflow and evidence boundaries.

## What works without broker credentials

The API, dashboard, tests, stored-data analysis, and backtests can run without Kite credentials. Kite credentials and a current daily access token are needed only for downloading or streaming broker data.

## Evidence-gated opportunities

The scanner still checks the entire configured watchlist. **Require qualified outcome evidence** is on by default: missing, failed, mismatched or expired evidence gives no eligible plan, not an invented win probability. Legacy ML/hybrid rankings remain exploratory. Separately trained BUY/SELL outcome models can qualify in **ML only** mode only after explicit historical and forward-paper promotion gates.

For a selected stock, use **Trade Outcome Validation** in the dashboard after importing sufficient history (at least 1,000 completed candles; qualifying usually needs much more). It replays fixed rules with next-open entries, ATR stops, targets, costs and session exits. It fits calibration on the middle window and evaluates it on a later untouched holdout. It does not retrain or replace the active model.

A qualified **Rules only** report supplies rule estimates for later candles with the same reward/risk; a promoted outcome model supplies model estimates under its frozen policy in **ML only** mode. Wait for a new completed candle, then scan again. Revalidate when evidence expires (at most seven days, also bounded by the data date) or the policy changes—not on every page refresh. Disabling the gate is explicitly exploratory. A high score or historical win rate does not promise a winning trade.

Freshness is rechecked when scanning finishes and whenever results are fetched. Minute signals expire after 2 minutes 15 seconds from their candle timestamp; other intervals use two bars plus 15 seconds. A long full-universe scan may therefore produce no fresh eligible picks. Incremental streaming scans and independent hybrid validation remain future work. See [USER_GUIDE.md](USER_GUIDE.md#trade-outcome-validation-and-win-estimates) for gates and assumptions.

## BUY/SELL outcome model lifecycle (optional research)

This is separate from legacy `optimize ml-model-v2`. It trains two calibrated profit-outcome classifiers with one shared feature/artifact contract, not a BUY/HOLD/SELL direction classifier.

The first five-stock candidate was trained on 2026-10-20 using 100,000 stored minute candles. **It failed qualification**: neither side found a tuning threshold meeting the win/expectancy/sample requirements. Both sides abstain, so test win rates are unavailable—not 0% or 100%. Nothing was promoted. The dashboard's **BUY / SELL Outcome Models** panel shows candidate results.

| When to run | Local command | Purpose |
|---|---|---|
| Inspect candidates or troubleshoot | `python -m trading_agent.strategy.outcome_cli status` | Shows active and candidate status; no training or promotion |
| Start a new experiment with sufficient history and a genuinely new test period; not daily | `python -m trading_agent.strategy.outcome_cli train RELIANCE TCS HDFCBANK ICICIBANK INFY` | Trains separate side models; saves a candidate without replacing the active model |
| During market sessions, **after historical gates pass**, with stored candles updating | `python -m trading_agent.strategy.outcome_cli paper CANDIDATE_ID --watch` | Records prospective shadow intents every 10 seconds and settles them as future candles arrive; Ctrl+C stops safely |
| Once both historical and forward gates pass; repeat only to renew qualified evidence or change candidate | `python -m trading_agent.strategy.outcome_cli promote CANDIDATE_ID` | Verifies evidence and atomically selects the candidate; there is no force flag |

Replace `CANDIDATE_ID` with a qualifying ID from `status`. Do not run the failed first candidate as if it had passed. To run any command in Docker, prefix the same Python command with `docker compose run --rm tools`, for example:

```powershell
docker compose run --rm tools python -m trading_agent.strategy.outcome_cli status
```

Rebuild after code changes with `docker compose build api frontend`, then `docker compose up -d api frontend`. No pruning, database reset or credential changes are required. After promotion, use **ML only**, **minute**, **2:1 reward/risk**, keep the evidence gate enabled and run a fresh scan. Predictions apply only to explicitly trained symbols; the first candidate does not cover all 2,319 stocks.

Both directions must independently exceed 60% wins, have Wilson and session-bootstrap 95% lower win bounds above 60%, and have positive bootstrap lower net expectancy. Historical minimum: 100 selected trades across 10 sessions per side. Forward minimum: 100 settled trades across 20 sessions per side. Pending, invalid, stale or missing evidence blocks promotion. These criteria do not guarantee future returns.

Artifacts, reports, reserved test periods and forward evidence live under `models/outcome_candidates/`; the active pointer is `models/outcome-active.json`. Back them up together. Do not delete the test-reservation ledger to rerun an inspected period. The CLI rejects overlapping test dates even if symbols change. See [USER_GUIDE.md](USER_GUIDE.md#buy-sell-outcome-models) for exact execution and evaluation assumptions.

## Quick start: local

Use Python 3.11 or 3.12 and Node 22 for the frontend. Docker and CI use Python 3.11. Tests also passed in the existing Python 3.13 environment, but use 3.11/3.12 for fresh installations of the pinned legacy-model dependencies. Only copy `.env.example` on first setup: do not overwrite your existing configuration.

```powershell
# Windows PowerShell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
Copy-Item .env.example .env

# Start the API; the SQLite schema is created automatically.
trading-agent-api
```

```bash
# macOS/Linux
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"
cp .env.example .env
trading-agent-api
```

Open:

- API: http://localhost:8000
- Swagger API documentation: http://localhost:8000/docs
- Health check: http://localhost:8000/api/v1/health

To run the dashboard in a second terminal:

```bash
cd frontend
npm ci
npm run dev
```

Then open http://localhost:5173. Vite proxies `/api` to the backend on port 8000. On PowerShell, use `npm.cmd` if execution policy blocks `npm.ps1`.

## Quick start: Docker

Docker Compose uses the same SQLite/Parquet storage model as local execution. `./data` and `./models` are mounted into every Python service. The frontend is a compiled bundle served by unprivileged Nginx, not a development server. Host ports are bound to loopback only.

```bash
cp .env.example .env       # PowerShell: Copy-Item .env.example .env
docker compose --profile live build
docker compose up -d api frontend
docker compose ps
```

Open the dashboard at http://localhost:5173 or the API docs at http://localhost:8000/docs.

The dashboard opens in the original research layout: watchlist sidebar, bullish/bearish **Top Opportunity** cards, **Plotly** chart, **Hybrid Decision Maker**, stop/target/position sizing, and news sentiment. Click **Scan entire watchlist** to evaluate every unique symbol in `symbols.txt`; the current file contains 2,319, not a hard-coded 2,270. Skipped symbols and data errors are reported. Refreshing the page does not launch a scan.

The existing `models/ml_model.joblib` is a legacy nine-feature classifier. The restored inference adapter reproduces its original feature formulas and class mapping. Use **1 minute** with this artifact; other intervals must use rules-only until a compatible artifact is supplied. Missing/incompatible models are explicitly unavailable, never fabricated predictions. The artifact lacks training provenance and interval metadata, so its performance must be revalidated; minute data follows the original loader's convention.

The saved classifier embeds scikit-learn **1.5.2**, now pinned in the project dependencies. Its Windows-specific `_loss` class alias is resolved explicitly on Linux without rewriting the model. Version-mismatched artifacts are rejected: [scikit-learn does not support loading persisted estimators across versions](https://scikit-learn.org/stable/model_persistence.html#security-maintainability-limitations). This compatibility fix is not proof of predictive performance. Docker caches dependencies/browser installation separately from source-only edits.

Choose capital, per-trade risk and target reward/risk before scanning. BUY/SELL disagreements produce HOLD. Stops use 1.5×ATR; targets use the selected reward/risk. The ranked basket reserves exposure and risk across both directions and excludes held/stale/over-budget entries from eligible top picks. Older signals remain visible in the sidebar as research only. Plans are snapshots, not executable quotes or broker orders.

**Refresh selected symbol news** fetches up to 10 recent headlines. Tick the LLM checkbox only if you want to invoke your configured provider (charges may apply). Mock mode supplies no real sentiment score. Scans consume cached sentiment only; they do not download candles or call an LLM for thousands of stocks. The **Paper operations** tab manages existing positions.

### Walk-Forward Optimizer (optional research)

Select a stock and interval, then use **Walk-Forward Optimizer → Run walk-forward evaluation**. This tunes rule thresholds on each training window and compares them with default rules on the following unseen test window. It does **not** train ML, change your active configuration, or place orders. Run it after adding history or when researching rule changes, not on every startup. Default windows require 4,500 candles (3,750 training + 750 test); the panel explicitly reports insufficient history or too few training trades.

For the same optional check from a terminal:

```powershell
# Local, with the virtual environment active
python -m trading_agent.research.walk_forward_optimizer RELIANCE --interval minute --train-candles 3750 --test-candles 750 --cost-bps 5

# Docker alternative; uses stored data, no login or download
docker compose run --rm tools python -m trading_agent.research.walk_forward_optimizer RELIANCE --interval minute --train-candles 3750 --test-candles 750 --cost-bps 5
```

Costs are an adjustable round-trip assumption in basis points, not an exchange/broker fee estimate. Training outcomes cannot cross into the test window. Both paths use next-bar open entry, fixed 12-bar exit and non-overlapping positions. Reported sums of trade returns are **not portfolio performance**. This is rule-only research, not validation of the hybrid ML model; retain a separate final holdout and see [USER_GUIDE.md](USER_GUIDE.md) for limitations.

The build command is required on first setup and after pulling or making application/dependency changes. It is not a daily command.

Compose overrides the local `.env` log-file setting and writes to `data/logs/trading-agent.log` on the host. This keeps file logging writable for the non-root container user and preserves logs across container replacement.

If startup reports `dependency failed to start` or an unhealthy API, run `docker compose logs --tail=100 api` to find the underlying error. For `PermissionError: ... 'logs'` from an older Compose configuration, update the project and run `docker compose up -d api frontend` to apply the corrected log path; no image rebuild or data deletion is needed for this configuration-only fix.

## Docker operating schedule

These are the commands an operator may need to repeat. Run them from the repository root.

| When | Required? | Command | Purpose |
|---|---|---|---|
| Before the first market-data operation of each trading day, or whenever Kite rejects the token | Yes, for broker data | `docker compose run --rm tools trading-agent login` | Automatically signs in and saves the current token in `data/.access_token` |
| After login on first use, after downtime, or when candles are missing | As needed | See **Catch up missing candles** below | Replays a one-day overlap and catches up the selected interval |
| Whenever the dashboard/API should run | Yes | `docker compose up -d api frontend` | Starts the API and dashboard without live broker processes |
| At the start of a live market-data session | Yes, for live mode | `docker compose --profile live up -d api frontend ticker scheduler` | Starts the complete application, live ticker, and scheduler |
| Immediately after startup | Recommended | `docker compose ps` | Confirms that the requested services started and the API is healthy |
| While monitoring or troubleshooting | Optional | `docker compose logs -f --tail=200 api frontend ticker scheduler` | Streams logs; press `Ctrl+C` to leave the log view without stopping containers |
| At the end of a session or before maintenance | Optional | `docker compose down` | Stops containers without deleting candle data, models, or the access token |
| After source, dependency, Dockerfile, or Compose changes | Required after such changes | `docker compose --profile live build` | Rebuilds images; rerun the appropriate `up -d` command afterward to replace old containers |

### Normal trading-day sequence

Authenticate first, catch up missing candles if the application was not continuously running, and then start live mode:

```bash
docker compose run --rm tools trading-agent login

# Run the catch-up command from the next section when needed.

docker compose --profile live up -d api frontend ticker scheduler
docker compose ps
docker compose logs -f --tail=200 api frontend ticker scheduler
```

The login command reuses a still-valid token when possible and automatically logs in when a new token is required. `Ctrl+C` only exits the final log-view command; the application continues running in Docker.

### Catch up missing candles

Run this after login when the system has been offline or a download was interrupted. Start with 100 symbols, review the summary, then omit `--limit` to process the complete watchlist. Choose the **same interval as `STORAGE_CANDLE_INTERVAL`** (default `5minute`); one-minute and five-minute datasets are separate and are not automatically resampled.

The command replays one day before each latest stored candle, correcting recent partial live bars and catching up through the last completed bar. A new symbol defaults to 30 days. Reads deduplicate retries by timestamp. This is not a complete interior-gap audit: use an explicit historical ingest for older holes. Physical storage row counts include replay copies until compaction.

Symbols that are no longer present in Kite's current NSE equity instrument list are logged as `gap_heal_symbol_skipped` and do not stop the remaining watchlist from being processed.

```bash
# Bounded validation first; replace 5minute with minute if configured that way.
docker compose run --rm tools trading-agent heal-gaps --interval 5minute --limit 100
# Full watchlist, only when needed:
docker compose run --rm tools trading-agent heal-gaps --interval 5minute
```

For a long run in Windows PowerShell, save the output so progress and failures can be reviewed later:

```powershell
docker compose run --rm tools trading-agent heal-gaps --interval 5minute --limit 100 *> data\backfill_latest.log
$LASTEXITCODE
Get-Content data\backfill_latest.log -Tail 100
```

If interrupted, rerun after checking authentication. Exit code 1 means failure; inspect `errors` and logs. Invalid instruments are listed under `skipped`; `rows_written` includes overlap, not just newly discovered candles. Authentication or open-breaker failures stop the batch. Investigate the first error before retrying; run only one historical-data job at a time because pacing is process-local.

### Other occasional Docker commands

```bash
# Run any CLI command in a temporary tools container
docker compose run --rm tools trading-agent --help
docker compose run --rm tools trading-agent storage-stats
docker compose run --rm tools trading-agent ingest RELIANCE TCS --days 30

# Inspect a specific service after an error
docker compose logs --tail=200 api
```

The `live` profile is deliberately opt-in because it uses broker/network resources and starts scheduled jobs.

## Kite setup and daily authentication

1. Copy `.env.example` to `.env`.
2. Set `KITE_API_KEY`, `KITE_API_SECRET`, `KITE_USER_ID`, `KITE_PASSWORD`, `KITE_TOTP_SECRET`, and the redirect URL registered with Kite.
3. Authenticate each trading day:

```bash
# Automated TOTP/Playwright flow
trading-agent login

# Manual browser flow if automation is unavailable
python manual_login.py
```

The generated token is stored at `data/.access_token`, which is ignored by Git and shared with Docker through the data mount.

For automatic login, install Chromium locally once:

```bash
playwright install chromium
```

## Common workflow

```bash
# Download history. Omitting symbols uses the configured/default watchlist.
trading-agent ingest RELIANCE TCS INFY --days 100
trading-agent backfill RELIANCE --years 2
trading-agent storage-stats

# Research stored data
trading-agent diagnose RELIANCE TCS
trading-agent backtest RELIANCE TCS --start 2025-01-01 --end 2025-06-30
trading-agent optimize walk-forward RELIANCE TCS
trading-agent optimize ml-model-v2 RELIANCE TCS

# One pipeline pass
trading-agent pipeline RELIANCE TCS

# Long-running market-hours processes
trading-agent-ticker
trading-agent-scheduler
```

Run `trading-agent --help` or `trading-agent COMMAND --help` for the exact options. `python -m trading_agent.cli` is an equivalent fallback when console scripts are not on `PATH`.

## Configuration

Settings come from process environment variables first, then `.env`. Important values are:

| Variable | Default | Purpose |
|---|---|---|
| `DB_URL` | `sqlite:///data/trading.db` | SQLAlchemy database URL |
| `DATA_DIR` | `data` | Runtime data and access-token directory |
| `STORAGE_PARQUET_ROOT` | `data/parquet` | Candle storage |
| `ML_MODEL_DIR` | `models` | Model registry and artifacts |
| `API_HOST` / `API_PORT` | `127.0.0.1` / `8000` | Local bind address; Compose overrides inside its containers |
| `API_AUTH_TOKEN` | empty | Optional bearer authentication; enable before sharing access |
| `STORAGE_CANDLE_INTERVAL` | `5minute` | Ticker, scheduler, and position-monitor interval |
| `PAPER_INITIAL_CAPITAL` | `1000000` | Initial paper capital; keep fixed for an existing ledger |
| `PAPER_ENTRIES_ENABLED` | `true` | Set `false` and restart API to block new paper entries (closing remains allowed) |
| `PAPER_MAX_POSITIONS` | `10` | Maximum open paper positions |
| `PAPER_MAX_EXPOSURE_PCT` / `PAPER_MAX_RISK_PCT` | `0.50` / `0.06` | Aggregate notional and stop-risk fractions of gross book equity |
| `PAPER_MAX_TRADE_RISK_PCT` | `0.02` | Maximum stop risk per paper entry |
| `API_CORS_ORIGINS` | local frontend origins | JSON list of allowed origins |
| `LOG_LEVEL` / `LOG_FORMAT` | `INFO` / `console` | Logging behavior |
| `LLM_PROVIDER` | `mock` | `mock`, `openai`, or `anthropic` |
| `LLM_BASE_URL` | OpenAI | Base URL for an OpenAI-compatible API |
| `LLM_API_KEY` / `LLM_MODEL` | empty / `gpt-4o-mini` | Optional LLM credentials/model |

Keep `LLM_PROVIDER=mock` to guarantee that the comparison path makes no external LLM calls. See [.env.example](.env.example) for every supported setting.

The dashboard shows its access-token form only when the server has `API_AUTH_TOKEN` configured; it stays hidden for ordinary local use without API authentication. A public `/api/v1/auth/status` endpoint returns only whether authentication is required, never the token. Supplied tokens stay in browser memory only. This is separate from Kite autologin, which handles broker access. Do not put secrets in frontend build variables. With authentication enabled, browser Swagger access also requires an Authorization header; ordinary direct navigation does not supply one. Remote access additionally needs TLS, an authenticated gateway, and an operator/security review.

Paper wallet totals come from persisted `open_positions` records, including closed rows. They are gross entry-cost accounting: no live mark-to-market, fees, slippage, deposits, or broker reconciliation. Manual closes use the price you enter. Stops generate advisory alerts, not guaranteed fills or automatic broker exits. Do not change initial capital to conceal losses.

## Architecture

```text
Kite/RSS -> ingest -> Parquet + SQLite
                         |
              core indicators/signals
                         |
        strategy (rule / ML / optional LLM)
                         |
             analysis + paper-risk layer
                         |
                 FastAPI -> React UI
```

The source layout is:

```text
src/trading_agent/
  api/        FastAPI app and routers
  analysis/   rankings, opportunities, sentiment, digests
  core/       config, indicators, signals, logging, resilience
  ingest/     Kite auth/history/live ticker, news, watchlists
  optimize/   optimization CLI
  research/   backtests, diagnostics, walk-forward studies
  risk/       paper wallet, portfolio limits, position monitor
  scripts/    scheduler, health checks, maintenance
  storage/    SQLAlchemy schema and Parquet store
  strategy/   candidate generation and decision paths
frontend/     React + TypeScript dashboard
migrations/   Alembic database migrations
tests/        unit and integration tests
```

SQLite is the supported runtime database today. PostgreSQL is not advertised until the remaining direct `sqlite3` consumers are migrated.

## Database lifecycle

The API creates missing tables on startup for a zero-setup local experience. For explicit migration-controlled environments, run:

```bash
alembic upgrade head
alembic current
# Docker equivalent (new databases / migration-managed deployments):
docker compose run --rm tools alembic upgrade head
```

Do not edit an already-applied initial migration in a deployed database; add a new Alembic revision instead.

For a new migration-managed database, run migrations **before** starting the API. An older auto-created database may have tables but no Alembic revision: back it up and compare its schema before adopting migrations; do not blindly stamp, drop tables, or run the initial creation over it. Stop writers before filesystem backups and verify restoration into a separate directory. `docker compose down` preserves bind-mounted data; image pruning is not required for an upgrade.

## Development checks

```bash
pytest
pytest --cov=trading_agent --cov-report=term-missing
ruff check src migrations

cd frontend
npm run typecheck
npm run build
```

The suite includes storage replay/concurrency, broker-message normalization, paper-ledger/API contracts, and fresh-database migration checks. See [READINESS.md](READINESS.md) for validation evidence and limitations; a build alone is not a live-session test.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `trading-agent` is not found | Activate the virtual environment and run `pip install -e .`, or use `python -m trading_agent.cli` |
| `No data available` | Authenticate, run `trading-agent ingest SYMBOL`, then check `trading-agent storage-stats` |
| Kite token rejected | Run `trading-agent login` again; Kite access tokens are short-lived |
| Playwright browser missing | Run `playwright install chromium`, or use `python manual_login.py` |
| Port 8000/5173 is busy | Change the port mapping in `docker-compose.yml` or use CLI/API port options |
| Docker frontend cannot reach API | Confirm `api` is healthy with `docker compose ps` and inspect `docker compose logs api` |
| SQLite is locked | Avoid multiple ad-hoc writers; stop duplicate ticker/scheduler processes |
| Rebuilt code appears unchanged | Rerun `docker compose up -d api frontend` (and live services if used); rebuilding alone does not replace running containers |
| `kite_historical` breaker is OPEN | Review the first underlying failure, check login, wait for broker recovery, then retry a bounded batch; avoid concurrent backfills |
| API returns 401 | Enter the configured bearer token in the console; never disable authentication to expose a public service |
| Paper entry returns 409 | Read the error: duplicate symbol, entry switch, capital, position, exposure, or stop-risk limit; closes remain available |

## Documentation map

- [USER_GUIDE.md](USER_GUIDE.md): detailed setup, daily operation, and recovery steps
- [READINESS.md](READINESS.md): integration audit, evidence, and institutional-readiness gates
- [ARCHITECTURE.md](ARCHITECTURE.md): architectural decisions
- [DESIGN.md](DESIGN.md): components, data flow, and deployment design
- [STRATEGIES.md](STRATEGIES.md): strategy hypotheses and promotion rules
- [EDGE_ANALYSIS.md](EDGE_ANALYSIS.md): measured signal-edge research
- [ML_REDESIGN_SUMMARY.md](ML_REDESIGN_SUMMARY.md): ML v2 design notes
- [PRD.md](PRD.md): product goals and constraints
- [RULES.md](RULES.md): contributor rules
- [TASKS.md](TASKS.md): active work and known limitations
- [MEMORY.md](MEMORY.md): historical decision log
- [TASKS_INTEGRATION.md](TASKS_INTEGRATION.md): archived integration implementation brief

## License

No license file is currently included. Treat the repository as all-rights-reserved unless the owner adds an explicit license.
