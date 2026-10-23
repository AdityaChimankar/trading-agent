# Trading Agent User Guide

This guide covers a first local run, Docker operation, the daily Kite workflow, research commands, and common recovery steps. For the shorter path, start with [README.md](README.md).

> Safety: the application records paper positions only. It has no real-order execution path.

Read [READINESS.md](READINESS.md) before unattended use. The current console is intentionally paper-only; production readiness, security, accounting, and live-session validation still have open gates.

## Research panels

The unified dashboard has a searchable watchlist sidebar and four connected steps: **Find opportunities → Inspect selected stock → Validate strategy → Paper review**. The top symbol picker and shared settings coordinate all steps. **Paper operations** uses the same selected symbol and candle interval; the old duplicate scanner has been removed from that console.

The opening overview groups paper equity, cash, positions, eligible picks and cached symbol headlines. Expand advanced settings to adjust sizing/evidence controls. Opening the overview or refreshing its views reads stored state; it does not download news, start training or run a new scan.

1. Set **Sizing capital**, **Candle interval**, **Decision mode**, **Risk per trade** and **Target reward/risk**. The legacy model in `models/ml_model.joblib` uses nine features and minute candles. Its training interval is inferred from the original code, not certified by artifact metadata. Do not interpret historical model metrics as validation of this deployment.
2. Click **Scan entire watchlist**. Every unique entry in `symbols.txt` is attempted (currently 2,319). Progress, stale counts, skips and failures are shown. Do not run the CLI scan simultaneously with a dashboard scan. Docker uses one API worker; this is not a distributed job system.
3. **Top Opportunity** shows the best eligible BUY and SELL plans after the complete scan. The sidebar retains directional historical/stale candidates, labelled as such. There may be no eligible pick: stale data, HOLD/conflict, missing model, an existing position or risk limits can prevent a proposal. Nothing is bought automatically.
4. Selecting a top pick opens **Inspect selected stock** with its Plotly candles, volume and indicators, rule markers, rule/ML evidence and the scan's stop/target/size. Sidebar selection preserves your current step so you can compare stocks inside validation. Changing settings requires another scan. A fresh chart does not make an old sizing snapshot current.
5. Under **Inspect selected stock**, click **Refresh selected symbol news** when needed. Optional **Analyze with configured LLM** makes one bounded provider request and may incur charges. Without a real provider, headlines are displayed without invented sentiment. Ticker searches can contain irrelevant headlines; review sources. Cached sentiment influences a later scan only if its analysis predates the signal candle and is no more than 48 hours old.
6. Open **Validate strategy** for the Walk-Forward Optimizer, Trade Outcome Validation and BUY/SELL Outcome Models. Walk-forward compares experimental rule thresholds against a baseline using fixed-horizon exits; it is not the stop/target qualification test and never applies parameters to the scanner. Rule outcome validation uses the shared reward/risk target. Promoted ML instead requires its fixed minute / ML only / 2:1 policy. Buttons offer those shared settings for review without launching a scan or promoting a model.
7. Open **Paper review** to inspect the persistent ledger or record a manual paper position. The shared stock is preselected, but entry, quantity, stop and target must be supplied explicitly. Switching stocks clears unsubmitted numeric fields to prevent transferring another stock's prices. This book does not substitute for the separate prospective outcome-model paper observer.

Walk-forward results are keyed by symbol, interval, training/test windows and costs; rule-validation results by symbol, interval and bracket policy. They stay available when moving between steps or visiting paper operations, but this browser result cache is not durable across a reload. An in-flight job keeps the inputs submitted with it even if you switch stocks. Navigating never launches a new evaluation. Failed requests remain errors, not a successful qualification.

After a validation completes, news is refreshed or a paper position changes, the workspace marks its existing scan plans as needing a new scan. It does not rewrite old rankings or silently recalculate sizing. A newly qualified report applies only to later candles; wait for those candles and explicitly re-scan. Full-universe scans continue in the background while you inspect or validate. Scores are not profit probabilities, and stale, incomplete, failed or mismatched evidence must not be treated as a current plan.

Sizing uses 1.5×ATR stops, the requested reward/risk target, integer shares, the configured single-stock notional cap (10% by default) and the paper book's exposure/risk/position-count limits. Both directional lists share reservations. Short-selling leverage, sector/correlation limits, taxes, fees and execution slippage are not modeled in these plans. A score measures rule/model evidence, not probability of profit.

`PAPER_MAX_SINGLE_POSITION_PCT=0.10` caps each symbol's notional. `PAPER_MAX_DAILY_LOSS_PCT=0.03` blocks new paper entries once the day's realized loss reaches 3% of IST session-start equity. `PAPER_MAX_DRAWDOWN_PCT=0.15` blocks entries at 15% realized drawdown from the ledger's peak, including previous sessions. These are fractions, not whole percentages. Existing positions can still be closed. The entry API checks the limits and inserts the position in one serialized SQLite transaction. Missing or invalid ledger evidence blocks entries; these limits do not measure unrealized losses or actual execution costs.

Each scan allocation records a fingerprint of the paper ledger, session date and risk settings. A later change from any client blocks that old allocation and asks for a re-scan. The original research result remains available. Keep initial capital fixed for an existing ledger; changing it redefines historical risk.

Scan snapshots and news caches live in `data/research/`; they survive Docker replacement. A scan interrupted by an API restart is marked interrupted and must be rerun. It is not resumed or silently reported complete. For an offline operator scan while no dashboard scan is running: `python -m trading_agent.analysis.research_jobs --interval minute --capital 100000` (or prefix with `docker compose run --rm tools` for Docker).

After installing changes to these panels and their API, run `docker compose build api frontend`, then `docker compose up -d api frontend`, and reload the browser. Do not rebuild for each scan.

## Optional automatic research

The API exposes `GET /api/v1/research/readiness` for storage coverage, feed evidence, scan freshness, model qualification and paper-risk status. `/api/v1/ready` is the smaller database health check; it does not mean a strategy is ready to trade.

Storage inventory runs in one background worker and is cached for up to 60 seconds. While it refreshes, `coverage.storage_status` is `pending`, counts are `null`, and the response includes `storage_pending`; refresh shortly to get observed counts. `error` means the directory inventory failed. `storage_observed_at` identifies the last completed observation. Inventory does not read or certify candle freshness.

Automatic research scans stored candles only and starts disabled after every API restart. In the API docs, call `POST /api/v1/research/automation/enable` with a body such as:

```json
{
  "scan": {
    "interval": "minute",
    "mode": "rule_only",
    "capital": 100000,
    "risk_pct": 1,
    "reward_risk": 2,
    "require_validated": true
  },
  "min_scan_seconds": 120
}
```

Use `GET /api/v1/research/automation` to inspect status and `POST /api/v1/research/automation/pause` to stop new automatic scans. Canceling a scan also pauses automation. The worker requires `API_WORKERS=1`, qualified evidence and the full configured universe; it supports minute, 5-minute and 15-minute data. It checks for completed-candle or ledger changes, waits for the configured weekday session, prevents overlapping scans and pauses after repeated failures. The delay is measured after completion. Settings can change after pausing.

This worker does not start ingestion, fetch news, train, promote or record trades. It uses the same conservative allocation gates as a manual scan. A large universe can still outlast a signal's validity window; automatic rescanning does not make historical or stale candles tradable. Session checks are weekday/time rules, without an exchange holiday calendar.

For a bounded exploratory comparison, `POST /api/v1/strategy-lab/compare` evaluates a fixed set of rule profiles and unconditional long/short baselines with consistent bracket fills and costs. It reads completed stored intraday candles, reports reference and evaluation periods, and permits one request at a time per API worker. The result cannot qualify a scan or promote a model. Returns and drawdown are additive equal-notional trade statistics, not compounded account performance; undefined metrics are `null` (shown as `n/a` in the CLI). Repeated inspection of its evaluation period is research selection, not an independent holdout.

## 1. Choose how to run it

| Mode | Best for | Requirements |
|---|---|---|
| Local Python | development, research, debugging | Python 3.11/3.12; Node 22 for UI |
| Docker Compose | repeatable API/UI and live services | Docker Engine/Desktop with Compose v2 |

Both modes use SQLite at `data/trading.db`, Parquet below `data/parquet`, models below `models`, and the token at `data/.access_token`.

## 2. Local installation

From the repository root:

```powershell
# Windows PowerShell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
Copy-Item .env.example .env
```

```bash
# macOS/Linux
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"
cp .env.example .env
```

Verify the installation:

```bash
trading-agent --help
python -c "import trading_agent; print(trading_agent.__version__)"
pytest
```

If the console command is not on `PATH`, use `python -m trading_agent.cli` in its place.

## 3. Configuration

Edit `.env`. The API and dashboard need no secrets. Broker ingestion requires at least:

```dotenv
KITE_API_KEY=replace_me
KITE_API_SECRET=replace_me
KITE_USER_ID=replace_me
KITE_PASSWORD=replace_me
KITE_TOTP_SECRET=replace_me
KITE_REDIRECT_URL=http://localhost
```

Keep this local default unless you intentionally move the database:

```dotenv
DB_URL=sqlite:///data/trading.db
```

The optional LLM path defaults to a local mock and makes no paid call:

```dotenv
LLM_PROVIDER=mock
LLM_BASE_URL=https://api.openai.com
LLM_API_KEY=
LLM_MODEL=gpt-4o-mini
```

To use an OpenAI-compatible provider, set `LLM_PROVIDER=openai`, its base URL, key, and model. Never commit `.env` or `data/.access_token`.

## 4. Start the local application

Terminal 1:

```bash
trading-agent-api
```

Terminal 2:

```bash
cd frontend
npm ci
npm run dev
```

Check these URLs:

| URL | Expected result |
|---|---|
| http://localhost:8000/api/v1/health | JSON with `status: healthy` |
| http://localhost:8000/api/v1/ready | JSON with `status: ready` |
| http://localhost:8000/docs | Interactive API docs |
| http://localhost:5173 | Dashboard |

The API creates missing tables on startup. Alembic can be used explicitly with `alembic upgrade head` in migration-controlled deployments.

## 5. Authenticate with Kite

Kite access tokens generally need refreshing each trading day.

```bash
# Automated browser/TOTP path
playwright install chromium   # first use only
trading-agent login

# Manual fallback
python manual_login.py
```

Both paths save the token under `data/.access_token`. Do not paste a token into logs, issues, screenshots, or documentation.

## 6. Load market data

```bash
# Specific symbols
trading-agent ingest RELIANCE TCS INFY --days 100

# Default watchlist
trading-agent ingest

# Longer history
trading-agent backfill RELIANCE TCS --years 2

# Inspect local Parquet storage
trading-agent storage-stats
```

The default watchlist comes from `symbols.txt`; named Nifty 500 loading uses `symbols_nifty500_plus.txt`. Explicit symbols are safest for the first run and avoid a large broker request.

## 7. Research and analysis

Run these only after the selected symbols have local Parquet data:

```bash
trading-agent diagnose RELIANCE TCS
trading-agent backtest RELIANCE TCS --start 2025-01-01 --end 2025-06-30
trading-agent optimize walk-forward RELIANCE TCS
trading-agent optimize walk-forward-single RELIANCE
trading-agent optimize ml-model-v2 RELIANCE TCS
trading-agent optimize strategy-lab RELIANCE --interval minute --max-bars 5000
```

Backtests now generate candidates from the time-bounded historical slice supplied by the engine; they do not read the latest future candle while replaying history.

The measured rule-based edge and strategy promotion constraints are documented in [EDGE_ANALYSIS.md](EDGE_ANALYSIS.md) and [STRATEGIES.md](STRATEGIES.md).

## 8. Run the paper-trading pipeline

For one pass:

```bash
trading-agent pipeline RELIANCE TCS
```

For market-hours operation, use separate terminals:

```bash
# Live Kite WebSocket -> Parquet candles
trading-agent-ticker

# News, decisions, monitoring, maintenance
trading-agent-scheduler

# API/dashboard backend
trading-agent-api
```

The scheduler uses the Asia/Kolkata timezone. It stores rule, ML, LLM, and shadow outputs independently so the paths can be compared. With `LLM_PROVIDER=mock`, the LLM path records mock HOLD decisions without a network charge.

Scheduler startup waits for configured job times; restarting it does not run an immediate news/sentiment/decision pass. Docker stop/SIGTERM shuts down the blocking scheduler and waits for active jobs. The ticker also handles SIGTERM and publishes a disconnected heartbeat on exit.

The ticker command refreshes socket health approximately every 15 seconds even without new market ticks. `connected` describes the actual socket and a recent process heartbeat; `last_tick` remains the actual exchange timestamp. A connection after market close does not imply fresh trading data. During market hours, the feed watchdog checks both connection status and tick age before requesting historical repair.

Set `STORAGE_CANDLE_INTERVAL` consistently for downloads, ticker, scheduler, monitor, and dashboard selection. Its default is `5minute`. Scheduled decision passes skip stale, incomplete, future-dated, and off-session inputs. The weekday/session guard does not know exchange holidays or special sessions.

After downtime, use a bounded replay first:

```bash
trading-agent heal-gaps --interval 5minute --limit 100
```

Omit `--limit` only after reviewing the result. This replays a one-day overlap and catches up the trailing gap, not every older interior gap. Read the error/skip summary; an exit code of 1 requires investigation. Run one downloader at a time.

## 9. Docker Compose

Prepare configuration first:

```bash
cp .env.example .env       # PowerShell: Copy-Item .env.example .env
docker compose up --build -d
docker compose ps
```

The default stack contains:

| Service | Purpose | Port/profile |
|---|---|---|
| `api` | FastAPI backend | 8000 |
| `frontend` | Compiled dashboard served by Nginx | loopback 5173 |
| `tools` | one-shot CLI container | `tools` profile / `docker compose run` |
| `ticker` | live Kite stream | `live` profile |
| `scheduler` | scheduled pipeline | `live` profile |

Common commands:

```bash
# CLI tasks
docker compose run --rm tools trading-agent --help
docker compose run --rm tools trading-agent login
docker compose run --rm tools trading-agent ingest RELIANCE TCS --days 100
docker compose run --rm tools trading-agent heal-gaps --interval 5minute --limit 100
docker compose run --rm tools trading-agent backtest RELIANCE --start 2025-01-01 --end 2025-06-30

# Enable long-running broker services only when ready
docker compose --profile live up -d ticker scheduler

# Observe and stop
docker compose logs -f api
docker compose logs -f ticker scheduler
docker compose down
```

`./data` and `./models` are bind-mounted. Stopping or rebuilding containers does not remove those host files. Docker Compose no longer starts unused PostgreSQL or Redis services; the application still has direct SQLite consumers and therefore officially supports SQLite.

## 10. CLI reference

| Command | Purpose |
|---|---|
| `trading-agent login` | Refresh Kite token using the automated flow |
| `trading-agent ingest [SYMBOL...]` | Fetch recent historical candles |
| `trading-agent backfill [SYMBOL...]` | Fetch a longer history |
| `trading-agent heal-gaps [SYMBOL...] --interval 5minute --limit 100` | Bounded catch-up with overlap, error summary, and failure exit status |
| `trading-agent storage-stats` | Summarize Parquet data |
| `trading-agent compact SYMBOL` | Compact a symbol's Parquet data |
| `trading-agent diagnose [SYMBOL...]` | Compare signal behavior with baselines |
| `trading-agent backtest SYMBOL...` | Replay a period using stored data |
| `trading-agent pipeline [SYMBOL...]` | Run candidate/opportunity processing once |
| `trading-agent news [SYMBOL...]` | Fetch current financial headlines |
| `trading-agent api` | Start the API through the main CLI |
| `trading-agent optimize ...` | Walk-forward, ML, and strategy-lab commands |

Always use `--help` on a command before a long or quota-consuming run.

## 11. Data and schema

- SQLite: configuration, paper positions, signals, model metadata, and monitoring records.
- Parquet: historical and live OHLCV candle series.
- Model directory: serialized ML models plus `registry.json`.
- Cache directory: downloaded instruments and derived caches.

Fresh databases can be created either by API startup or by:

```bash
alembic upgrade head
```

If an initial migration has already been applied elsewhere, create a new revision for schema changes rather than rewriting history.

For a migration-managed fresh database, run Alembic before the API creates tables. Existing auto-created databases need backup and schema comparison before migration adoption; do not stamp an unverified schema. For Docker use `docker compose run --rm tools alembic upgrade head` only under that lifecycle.

Paper book balances now derive from persistent `open_positions` rows (open and closed), not a separate in-memory wallet. They exclude fees, slippage and unrealized P&L. Keep `PAPER_INITIAL_CAPITAL` fixed for an existing book. `PAPER_ENTRIES_ENABLED=false`, followed by an API restart, blocks new entries but allows exits. Exposure/risk limits are documented in the README. Advisory monitoring does not automatically execute stop orders.

Optional `API_AUTH_TOKEN` protects reads and writes. Enter it in the dashboard access-token form; it lasts for the current page session only. The default stack is localhost-only. Do not expose it publicly without TLS, proper identity management, and review.

## 12. Tests and developer checks

```bash
# Backend
pytest
pytest --cov=trading_agent --cov-report=html
ruff check src migrations

# Frontend
cd frontend
npm run typecheck
npm run build
```

CI uses Python 3.11, SQLite, the backend tests, and the frontend production build.

## 13. Troubleshooting

### Import or command not found

Activate the intended virtual environment and reinstall from the repository root:

```bash
pip install -e ".[dev]"
python -m trading_agent.cli --help
```

### API is healthy but dashboard is empty

This is normal on a fresh installation. Authenticate and ingest one or two explicit symbols, then reload the dashboard.

### Readiness says database is unavailable

Confirm `DB_URL=sqlite:///data/trading.db`, the `data` directory is writable, and only one intended stack is running. The readiness query uses SQLAlchemy 2-compatible SQL.

### Kite login fails

- Confirm every `KITE_*` value is set and the redirect URL exactly matches the Kite app.
- Run `playwright install chromium` for automatic login.
- Use `python manual_login.py` when browser automation changes or is blocked.
- Generate a new request token; request tokens expire quickly.

For developers investigating changed login-form selectors, the optional manual
diagnostic lives in `src/trading_agent/scripts/inspect_kite_login.py`:

```bash
python -m trading_agent.scripts.inspect_kite_login --help
# Contacts Kite and lists input metadata; does not submit credentials by default.
python -m trading_agent.scripts.inspect_kite_login
# Optional: submit configured user ID/password to inspect the next form; no TOTP.
python -m trading_agent.scripts.inspect_kite_login --submit-credentials
```

Add `--headed` only when you want a visible browser. This is not a daily startup
step or an automated test. It does not save a session token, print input values,
or confirm authentication. For normal authentication use `trading-agent login`.

### Docker service exits

```bash
docker compose ps -a
docker compose logs api
docker compose run --rm tools trading-agent --help
```

If `ticker` exits, refresh the Kite token and restart only that service.

### SQLite lock errors

Stop duplicate local/Docker schedulers or tickers. Keep the database on a local disk, not a network share. PostgreSQL is not yet a drop-in replacement because maintenance and health modules still use SQLite-specific queries.

### Research worker still stopping

If an in-process API restart reports that the previous research worker is still stopping, wait for its pending local storage read to finish before retrying, or restart the API process. A stopping worker is not reported as running, and automatic research must be enabled again after a successful restart.

### Dashboard entry point

The restored `ResearchDashboard` uses `/api/v1/research` for aligned rule/ML analysis, full-watchlist scans, Plotly charts and optional cached news sentiment. Find, Inspect, Validate and Paper share the current symbol and interval. The Paper tab manages the persistent paper book through `OperatingConsole`; research actions remain in the shared workflow. Unreachable legacy dashboard files have been removed; their historical versions remain available on `rework` and in Git history.

### Walk-Forward Optimizer

Use this optional research panel after importing enough history or investigating a rule change. Select the stock and interval at the top of the Research dashboard. Set training candles (250–10,000), test candles (100–5,000), and assumed total round-trip cost (0–100 bps), then click **Run walk-forward evaluation**. Nothing runs on page load. The calculation is bounded to one stock and its latest 20,000 completed stored candles; the API permits one optimizer request at a time per worker. Keep `API_WORKERS=1`.

Each fold selects among 27 combinations of pattern-confirmation BUY RSI, SELL RSI and ADX thresholds using only its training outcomes. At least 15 non-overlapping training trades are required. The next window tests that choice against default rules; test windows do not overlap. The last 12 possible signal bars are purged from each window so no outcome crosses its boundary. Indicators use past candles and confirmed swings. Both train and test enter at the next bar's open, exit 12 bars after the signal, and subtract the same cost assumption.

The table shows out-of-sample trade count, win rate, mean net trade return, additive return sum and additive drawdown. Zero qualifying folds is not a successful validation. Inspect fold dates and chosen parameters in the details section. Changing inputs does not retroactively change results; rerun explicitly. Results stay in the page session and are not promoted into rules, models or paper positions.

Limitations: rules only, no ML fitting or news replay; no stop/target execution, session-boundary exits, historical universe/corporate-action adjustment, liquidity, taxes, short financing or portfolio allocation simulation. Repeatedly tuning after reading test results can still overfit. Do not interpret the additive return sum as a compounded account return. Five bps is a configurable research assumption, not a verified fee schedule. CLI and Docker equivalents are in the README; `--all` is an explicit, potentially expensive offline watchlist evaluation, not a daily startup step.

### Trade Outcome Validation and win estimates

This panel is separate from the Walk-Forward Optimizer. It evaluates the actual fixed-rule stop/target policy, not the optimizer's fixed-horizon exits and not the legacy ML artifact.

1. Import stored intraday history, select a symbol and interval, and set reward/risk in Opportunity Finder.
2. Click **Validate SYMBOL outcomes**. This runs only on request, uses at most 20,000 completed candles, saves a report under `data/research/validation`, and does not call Kite, an LLM or a broker order API.
3. Inspect BUY and SELL separately. Qualification needs at least 100 calibration and 100 holdout trades per side, at least 20 winners and 20 non-winners in calibration, at least 10 holdout sessions, a positive session-bootstrap lower expectancy bound, and Brier error below a constant-probability baseline.
4. If qualified, select **Rules only**, leave **Require qualified outcome evidence** enabled, wait for a later completed candle and run the full-watchlist scan. Evidence cannot qualify candles included in its own evaluation.
5. Repeat validation after substantial new history, policy changes or expiry. A report expires no later than seven days after its last data candle. Do not keep adjusting inputs until an inspected holdout passes: that creates selection bias.

Defaults: next-open entry, 1.5 ATR stop, selected reward/risk target, 24-bar maximum horizon; 10 bps round-trip cost plus 2 bps adverse slippage on each fill. Gap stops fill at the worse open. If both barriers touch in one candle, the stop wins. Positions exit at the last fully observable bar ending by 15:25 IST; entries stop before 15:20. Larger intervals may require earlier exits. Missing outcome bars are excluded, not counted as losses. These are simulation assumptions, not verified transaction charges or execution guarantees.

The oldest 60% is reference data, the next 20% calibrates the rule score, and the newest 20% is holdout. Label horizons are purged at boundaries and trades do not overlap within each symbol/window. Wilson win-rate intervals describe historical samples, not the uncertainty of an individual prediction. Session-block bootstrapping only partly addresses dependent outcomes. Universe-wide multiple testing, market/sector drift, corporate actions and survivorship bias are not corrected.

The API is `POST /api/v1/research/outcome-validation` (see `/docs`); custom cost/slippage/horizon reports are research-only because the scanner currently accepts the defaults above. One report per symbol/interval is retained, so a new request replaces the previous report. Only one validation runs per API worker; keep the supported single-worker deployment.

Hybrid and ML estimates remain unavailable until an independently trained, time-provenanced model is validated. Disabling the evidence checkbox permits **exploratory** plans, not validated recommendations. Eligibility also requires current session hours, fresh candles, traded volume, an average 20-bar turnover proxy of INR 100,000/bar, no recent same-day gaps, and existing portfolio limits. These filters are not order-book liquidity checks or an official exchange calendar. No eligible result is a valid outcome.

No extra daily startup command is required. After updating this code, rebuild once with `docker compose build api frontend`, then `docker compose up -d api frontend`; for local use restart the API and frontend. Use validation and scan buttons only when their data/policy conditions above require them.

### BUY/SELL outcome models

The new lifecycle is `python -m trading_agent.strategy.outcome_cli`; the older `optimize ml-model-v2` pipeline does not produce this dashboard contract.

The fixed candidate configuration uses two HistGradientBoosting binary classifiers, one per side, with 13 causal features. A separate logistic calibrator per side maps classifier output to the observed after-cost win label. A complete label requires known stop/target/timeout outcomes; missing outcomes are excluded, not relabeled as losses.

All selected symbols share session-based chronological boundaries: oldest 50% training, next 15% probability calibration, next 15% threshold selection, newest 20% final historical evaluation. Full label horizons are purged at boundaries. Fixed model settings do not use early stopping on the test set. Thresholds are selected only from the predeclared 0.60–0.90 grid on tuning data with at least 30 selected trades and five sessions. If no threshold passes, the internal `1.01` sentinel means **abstain**, not a 101% predicted chance. If both sides pass their thresholds on the same candle, the shared policy abstains.

Historical execution, prospective paper and inference share one hashed feature/policy contract:

- Minute candles, completed bars only; no imputation of missing required features.
- Entry at signal timestamp + two minutes: one full bar of decision latency. Example: the 10:00 candle closes at 10:01, the intent is recorded between 10:01 and 10:02, and entry uses the 10:02 open. Historical replay uses the same delay.
- Stop distance 1.5 signal ATR, target 2:1, at most 24 bars from entry. Stops win ambiguous bars; gap stops use the worse open.
- Round-trip cost assumption 10 bps, plus 2 bps adverse slippage per fill. Forced session exit no later than 15:25 IST; no overnight positions.
- Volume, turnover, recent-bar continuity and session filters shared across training/paper/inference. One position at a time per symbol in evaluation; this is not a capital-constrained multi-symbol portfolio backtest.

Test dates are reserved before fitting and are not reusable as a new holdout, including after interruption or failure. Never tune repeatedly on final test results. Some stored history has already been inspected with older strategies; a fresh split does not erase that research-selection risk. Mandatory **prospective** paper evidence is the final independent confirmation, not a claim that historical market data has never been seen before.

`paper CANDIDATE_ID --watch` uses an internal UTC clock, records immutable signal-time features before the future entry bar opens, then settles from later completed candles into that candidate's own SQLite ledger. It neither writes the normal paper book nor places orders. Stale stored candles cannot create retroactive forward records. Missed observation periods are not backfilled. Missing or corrected outcome data can invalidate evidence and block promotion; incomplete outcomes remain pending. Keep a single observer running during the sessions being evaluated and keep candle ingestion current. No all-universe throughput or order-book-fill guarantee is implied.

Promotion requires both side gates described in the README. It rechecks the artifact checksum, source/policy contract, historical trades and forward ledger; no force override is provided. The active pointer expires within seven days and cannot be renewed using stale forward outcomes. Source-contract changes invalidate older artifacts. A broken/expired active pointer blocks ML inference rather than silently reverting to the legacy model. Without an active pointer the legacy model remains available as unvalidated exploratory inference.

The dashboard shows candidate status and uses only explicitly promoted candidates in **ML only** mode. Rules+ML hybrid remains exploratory because adding rule arbitration would change the evaluated selection policy. Changing the reward/risk from 2:1 also invalidates model evidence for sizing. Outcome-model signal plans expire when their delayed entry bar opens, without the extra 15-second grace used by legacy research.

The initial five-stock experiment failed its tuning gates, so it has no eligible test trades and must not be promoted. Improving features/data is development work; a subsequent experiment still needs a later uninspected test period and future paper sessions. Neither increasing a displayed confidence score nor deleting a failed report establishes a 60% win rate.

## 14. Safe shutdown

### Database upgrades

Fresh databases and existing installations need separate validation. `create_all` creates missing tables but does not upgrade existing columns. The `/api/v1/ready` endpoint now resolves every mapped table/column and returns 503 for an incompatible schema; `/health` remains a process liveness check.

Migration `002` upgrades previously deployed revision `001` databases that used physical `metadata` columns in positions, signals, opportunities and system events. It renames those columns without replacing their contents and adds `trades.updated_at`, backfilled from each record's `created_at`. Fresh canonical schemas require no changes. Ambiguous metadata layouts are rejected for manual inspection.

Before upgrading an existing database, stop API/scheduler writers and create a consistent SQLite backup with `sqlite3.Connection.backup` (copying only the `.db` file while WAL writes are active is insufficient). Verify the migration on a copy and retain the backup. With the repaired image built, run `docker compose run --rm tools alembic upgrade head`, then start the API and scheduler and verify `/ready`, `/positions/` and `/wallet/summary`. In the linked-worktree layout, add `--project-directory . -f ./.worktrees/gemini-repair/docker-compose.yml` to Compose commands from the main folder. Never stamp a populated legacy database as current without verifying its schema. Downgrading revision `002` retains canonical columns and their contents; restoring the exact earlier physical schema requires the backup.

Use `Ctrl+C` for local processes. For Docker:

```bash
docker compose down
```

Project data is stored in bind-mounted `./data` and `./models`; back those up before manual deletion. Routine upgrades need a rebuild and `up -d`, not image pruning or data removal. See the README operating schedule for when each command is needed.
