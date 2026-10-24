# Project memory

This file is the **single source of truth for project status**: what exists, what is verified, what is open, and why past decisions were made. Update it in the same commit as any change to features, priorities or verification evidence. Other documents describe *how* to run and *how* it is built; they link here for status instead of keeping their own task lists.

| Need | Document |
|---|---|
| Quick start and daily commands | [README.md](README.md) |
| Detailed operation and recovery | [USER_GUIDE.md](USER_GUIDE.md) |
| Binding design decisions | [ARCHITECTURE.md](ARCHITECTURE.md) |
| Components and data flow | [DESIGN.md](DESIGN.md) |
| Verification commands, evidence and release gates | [READINESS.md](READINESS.md) |
| Product scope | [PRD.md](PRD.md) |
| Contributor rules | [RULES.md](RULES.md) |
| Strategy hypotheses / historical edge research | [STRATEGIES.md](STRATEGIES.md), [EDGE_ANALYSIS.md](EDGE_ANALYSIS.md) |

## Current status

Last updated: 2026-10-24 (strategy search with frozen selection; realistic cross-sectional re-test). Active branch: `unified-platform` (from `gemini`), not yet pushed or merged.

The application is a single intraday NSE workbench: Kite data pipeline, rule/ML/LLM analysis, classic and research dashboards, a persistent paper book, intraday risk controls, a guarded order router, performance analytics, volume-based recommendations and ML drift monitoring.

**Verified** (2026-10-24, after the strategy-search work): 523 Python tests passed three runs in a row (`PYTHONPATH=src`), repo-wide Ruff passed, the frontend type-check and production build passed, and both offline browser smokes passed. An earlier Playwright pass on a copy of the real 2026-10-21 scan and stored candles covered all eight dashboard routes with no console errors, failed API calls or mobile overflow.

**Not verified:**

- Live Zerodha orders and broker-side reconciliation (tested only against a stubbed Kite client).
- A full live market session with ticker, scheduler, square-off and drift check running.
- The cost model against the current Zerodha charge sheet.
- Any trading edge. Volume signals are rules of thumb. The first BUY/SELL outcome-model candidate failed qualification. Rule walk-forward results on RELIANCE were negative after costs.

Keep paper mode until those are evidenced.

## Feature map

| Area | Feature | Where |
|---|---|---|
| Data | Chunked, resumable, rate-limited Kite backfill; one-day overlap gap healing | `ingest/`, `trading-agent backfill / heal-gaps` |
| Data | Live WebSocket candles; DB writes in a background thread (prevents 1006 disconnects) | `ingest/live_ticker.py` (`BackgroundCandleWriter`) |
| Data | Nifty 500 watchlist builder with automatic instrument-token lookup and stale-cache fallback | `ingest/build_nifty500_symbols.py`, `ingest/instrument_lookup.py` |
| Data | RSS news matched to the watchlist | `ingest/fetch_news.py` |
| Data | SQLite WAL, `busy_timeout`, Alembic migrations 001–003; immutable Parquet batches | `storage/db.py`, `storage/parquet_store.py`, `migrations/` |
| Data | Pre-flight check (env, Kite, DB WAL, watchlist tokens, RSS, LLM) | `python -m trading_agent.scripts.validate_setup [--skip-llm]` |
| Analysis | RSI, ATR, ADX, MACD, Stochastic, Williams %R, CCI, +DI/-DI | `core/quant_indicators.py` |
| Analysis | Doji, hammer, engulfing, double top/bottom | `core/pattern_detection.py` |
| Analysis | Groww-style technical summary and floor pivots (P, R1–R3, S1–S3 from the previous session), shown in the symbol view | `analysis/technical_summary.py`, `components/TechnicalsPanel.tsx`, `GET /api/symbols/{s}/technicals` |
| Analysis | LLM headline sentiment through OpenRouter, rate limited, 429/5xx retry with `Retry-After` | `core/llm_client.py`, `analysis/research_news.py` |
| Decisions | Rule agent is the validated path; LLM and ML run in parallel in separate tables and never override it | `strategy/`, `signals` / `llm_signals` / `ml_signals` |
| Decisions | Full-universe research scan, watchlist bias, contradictions, Top Opportunity by conviction and room to S/R | `analysis/research_jobs.py`, `api/routers/legacy_ui.py` |
| Risk | ATR stop/target/sizing, portfolio exposure/risk caps, realized daily-loss and drawdown gates | `risk/`, `risk/ledger_guard.py` |
| Risk | Kill switch (persisted, fails closed), session gates (09:15 / 15:00 cutoff / weekends), trade cap, optional one entry per symbol per day, 15:15 square-off, JSONL audit | `risk/controls.py`, scheduler `square_off_cycle` |
| Execution | Order router: risk checks before any order, paper fills with slippage, live Kite MARKET/MIS behind double opt-in, reconciliation | `execution/router.py`, `execution/reconcile.py` |
| Execution | Zerodha intraday cost model | `execution/costs.py` |
| Analytics | Net-of-cost performance, Sharpe/Sortino/drawdown/profit factor/expectancy/R, attribution | `analysis/performance.py` |
| Analytics | RVOL by time of day, VWAP, liquidity tiers, participation cap, confirm/contradict vs scan | `analysis/volume_insights.py` |
| Analytics | U-shape remaining-volume forecast with out-of-sample check; router caps size at 5% of forecast remaining volume | `volume_insights.forecast_day_volume / evaluate_volume_forecast`, `/api/v1/analytics/volume/{s}/forecast-check` |
| Research | Cross-sectional ranking (Ghosh et al. 2021) walk-forward vs random picks, 15:15 MIS exit, >= INR 10 crore liquidity filter, research gate, long/short list only when passed | `research/cross_sectional.py`, `trading-agent optimize cross-sectional`, `/api/v1/analytics/cross-sectional`, Analytics panel |
| Research | Strategy search: 2,508 entry x bar-size x exit candidates; selection on development/validation only (Bonferroni clustered t, session bootstrap, symbol stability, always-long/short/random baselines, 10+2x2 bps) frozen to a hashed run file before holdout; rolling walk-forward over exit policies and all candidates; immutable runs with git revision, versions and data fingerprints | `research/strategy_search.py`, `trading-agent optimize strategy-search` |
| Research | Strategy Lab moving-average profiles (`rule_ma` EMA9/21 filter on the rule, `ema_cross` EMA9/21 crossover + VWAP); EMA9, EMA21 and session VWAP on the chart | `core/signals.ma_trend / apply_ma_filter`, `research/strategy_lab.py`, `PriceChart.tsx` |
| Research | Strategy Lab opening-range breakout and Nifty-gap-regime profiles; NIFTY50/BANKNIFTY index candles | `research/strategy_lab.py`, `ingest/instrument_lookup.INDEX_TOKENS` |
| ML | PSI/KS drift monitor; withdraws ML (or loads `ml_model.previous.joblib`) on drift; daily 15:40 check | `strategy/model_monitoring.py` |
| Validation | Vectorized backtest `--selftest` vs loop reference, walk-forward `--all`, lookahead and train/serve skew checks | `research/walk_forward_optimizer.py`, `strategy/session_features.py` |
| UI | React + MUI: Dashboard, Research, Paper operations, Risk & execution, Analytics, Signal Pipeline, Transactions, News | `frontend/src/App.tsx`, `lib/router.ts` |

## Open work

| Priority | Task | Done when |
|---|---|---|
| P0 | Live-session certification | ticker, scheduler, square-off and drift check run through a full session with recorded heartbeats and no unexplained positions |
| P0 | Small live-order test | one supervised live round trip; reconciliation matches broker positions; audit log complete |
| P0 | Verify charge rates | `execution/costs.py` matches zerodha.com/charges on the test date |
| P0 | Build the drift baseline on real data | `python -m trading_agent.strategy.model_monitoring baseline` run; `/api/v1/analytics/drift` reports `ok`/`warn`, not "no baseline" |
| P0 | Broker order lifecycle hardening | partial fills, rejects, cancels, reconnect replay and idempotent intent IDs covered by fault-injection tests |
| P1 | Prospective paper evidence | ≥ 20 trading days of paper results so analytics stop warning about sample size |
| P0 | Re-run research on corrected timestamps | earlier walk-forward, outcome-validation and outcome-model results were computed on history whose legacy sessions were shifted 5h30m; rerun before relying on any of them |
| P1 | Ingest Nifty 50 candles and run `orb` / `orb_gap` in Strategy Lab | NIFTY50 minute candles stored; profiles compared with baselines after costs on several symbols |
| P0 | Prospective paper trial of the cross-sectional ranker | the frozen defaults (lags 10, train 60, top/bottom 10, 15:15 exit, >= INR 10 crore) recorded daily from a fixed start date, with no parameter changes; >= 60 sessions; net mean > 0 with session-clustered t >= 2 after real fills. Every historical window has now been inspected, so only prospective sessions are clean evidence |
| P1 | Cross-sectional ranking on longer history | daily or intraday history long enough for `--max-lag 240`; point-in-time Nifty 500 membership to remove survivorship bias |
| P1 | Exchange holiday calendar | session gates and scheduler skip NSE holidays and special sessions |
| P1 | Finish MUI conversion | remaining `.btn` and legacy markup in older pages replaced by MUI components |
| P1 | Lint/type baselines | selected Ruff rules and type checks pass and become required CI gates |
| P2 | Incremental scans | full-universe scan latency fits the signal validity window |
| P2 | Exposure controls | sector/correlation limits and stress scenarios |
| P2 | Database roadmap | either complete PostgreSQL support or keep SQLite-only claims |

## Known limitations

| Limitation | Workaround |
|---|---|
| Kite token expires daily | `trading-agent login` or `python manual_login.py` |
| SQLite writer concurrency is limited | one API worker, one ticker, one scheduler, one downloader |
| Session gates are weekday/time rules, not an exchange calendar | check holidays manually |
| Ledger limits use realized gross P&L | unrealized losses are not in the daily-loss gate |
| Paper fills are modelled (5 bps slippage) | treat paper P&L as optimistic |
| RVOL needs ≥ 5 prior sessions of minute data | the page flags unreliable values |
| Legacy ML artifact has no training provenance | treat ML output as exploratory; rule path is authoritative |
| `.test-tmp*` and `.pytest_cache` in the worktree have locked ACLs | gitignored; delete with elevated permissions if needed |

## Decision log

Newest first. Each entry records what changed and why; verification counts were true on that date only.

### 2026-10-24: Strategy search (frozen selection) and realistic cross-sectional re-test

**Request.** Validate all strategies, tune the ML and other approaches, and find a more profitable, higher-win approach.

**Review fixes.** A first three-way sweep was reviewed and had six real defects:

- one fixed split instead of walk-forward;
- holdout computed for every candidate, which turned it into another tuning window;
- the rule as the only baseline;
- a trade-level t-stat on correlated trades;
- selection at optimistic costs (6+2×2 bps);
- an unused `min_val_trades` and no run provenance.

`research/strategy_search.py` was rewritten to fix them:

- Selection uses development and validation only, at 10+2×2 bps. Its gates are a Bonferroni bar over every candidate, a session-clustered t-stat, a session bootstrap, symbol stability, ≥ 100 validation trades, and beating the always-long, always-short and random-entry baselines.
- The shortlist is frozen to `runs/<id>/selection.json` (SHA-256) before any holdout replay.
- Rolling walk-forward runs inside development and validation, over exit policies and over all non-ML candidates.
- Runs are immutable and record git revision, versions, data fingerprints and skipped symbols.
- Tests cover holdout isolation (rules and ML), freezing order, gates, prefix causality, session ownership, cost shifts, walk-forward choice and provenance.

**Result.** Run `20261024T053143Z-d9b334d3`: 30 symbols; 151/51/51 sessions; holdout from 2026-07-27.

- 2,508 candidates; Bonferroni t ≥ 4.11. **0 shortlisted and 0 nominally positive.** The holdout was therefore not scored.
- **Exit research (closes the P1 task):** walk-forward over the rule's exit policies was −13.5 bps/trade out of sample (7,449 trades, 95% CI −15.0 to −11.9). At size-aware costs (9.9 bps at ₹2 lakh) that is about −9.4 bps. The chosen exits drifted between folds.
- Walk-forward over all non-ML candidates was −15.1 bps.
- ML scored a 68% win rate on development but 38% on validation, worse than the baselines.
- Before costs, every family was within a few bps of the unconditional baselines.

**Conclusion.** Single-stock intraday timing on these bars has no edge after costs. A higher win rate is available (tight targets, wide stops) only at negative expectancy.

**Cross-sectional ranking**, realistic re-test (15:15 MIS exit, ≥ ₹10 crore 20-day mean traded value, 15 bps, 9 configurations fixed in advance, 60-day training). The walk-forward test days were split into a validation half (2026-01 to 05) and a holdout half (2026-05 to 10).

- The holdout was positive in 9/9 configurations; random picks lost −14 to −16 bps/day.
- The validation pick (lags 10, top 10) made +12.5 bps/day on validation (t 2.24). On the holdout it made **+11.2 bps/day**, with a 59.5% hit rate (t 1.82). These became the module defaults.
- **Correction:** an earlier summary of this tuning reported a "validation-selected" +7.7 bps result. A key bug in the selection script had made it pick the first configuration, not the validation best.

**Caveats.**

- Every historical window, including both holdouts, has now been inspected in this session, so no further historical number is clean.
- Survivorship bias remains (today's Nifty 500).
- Fills at exactly the open (NSE pre-open auction) and at 15:15 are assumptions.

**Decision.** Nothing is wired into live trading. The next evidence must be a prospective paper trial of the frozen ranker (see Open work).

**Also fixed:**

- Temp file names were made short. A long `DATA_DIR` had pushed the selection file past Windows' 260-character path limit and crashed a run before the freeze.
- Two pre-existing timing-flaky tests now pin file modification times.

### 2026-10-24: Moving averages in the rule path (tested, not adopted)

The user asked whether moving averages should be added to the rule. Added EMA9, EMA21 and session VWAP to the shared feature frame and the chart, plus two Strategy Lab profiles: `rule_ma` (rule + EMA9/21 alignment) and `ema_cross` (crossover + ADX ≥ 20 + VWAP side).

Tested on 20 large NSE symbols: newest 40% of up to 10,000 bars, next-open entry, 1.5 ATR stop, 2R target, 24 bars, 10 bps + 2×2 bps.

- Gross per-trade results were within ±3 bps for every variant, including the unconditional baselines.
- Net per-trade results ran from −12.4 to −14.7 bps on 1-minute bars and from −13.2 to −16.6 bps on 5-minute bars.
- The filters removed 70–90% of trades for about 1 bp of change.
- The strict "price above both EMAs" filter removed every signal, because the rule buys pullbacks.

**Decision:** keep the live rule unchanged, and keep the profiles for research.

**Lesson:** on these bars, costs (about 14 bps a round trip) dominate any entry filter. The lever is fewer, larger moves (longer holds or higher timeframes), not a better entry filter.

### 2026-10-24: Volume forecast, opening-range breakout, cross-sectional ranking; legacy timestamp fix

Built the three queued adaptations:

- **Volume forecast:**
  - The U-shape forecast blends the median share-by-minute profile with today's pace, weighted by the square root of the share already traded.
  - On 15 large symbols (about 3,000 sessions) it beat both the flat-average and straight-line-pace baselines at 10:15, 12:00 and 14:00. The weight was chosen among three variants on that same data, so the result is in-sample.
  - The router adds a 5% cap on forecast remaining volume.
- **Strategy Lab:**
  - New `orb` (09:15–09:30 range, first close beyond it) and `orb_gap` (filtered by the Nifty 50 opening gap, ±0.2%) profiles.
  - NIFTY50 and BANKNIFTY index tokens are now downloadable.
- **Cross-sectional ranking:**
  - A walk-forward random-forest ranker with a random-pick baseline and a research gate.
  - A long/short list is published only when the gate passes. Analytics has a panel and a Run button.

First real-data ranking run (2026-10-24, on corrected timestamps): 494 of 501 Nifty 500 symbols had data, and 228 sessions were usable from stored minute candles. Settings: lags 1–20, 120-day training window, top/bottom 10, 15 bps costs. Across 108 out-of-sample test days (2026-04-29 to 2026-10-01) the model book netted **+10.6 bps/day**, with a 55.6% hit rate, t-stat **1.95**, max drawdown 2.8 points, and 53.9% classifier accuracy. Random picks netted −15.2 bps/day, which is just the cost. **Research gate not passed** (t-stat < 2), so no list was published. This is promising but not significant, and it carries survivorship bias. Rerun as more history accrues and before any paper trial. (The random baseline averages 200 draws per day, so only its mean is meaningful; its hit rate and t-stat are not.)

Found and fixed a store-wide data bug. Legacy candle files (`data.parquet` from the old pipeline) hold naive IST strings, and the reader parsed them as UTC, so every legacy session appeared 5h30m late (14:45–21:00 IST). A scan of the store found only two formats, naive strings and aware UTC, so the reader now reads naive stored values as IST. Nothing is rewritten on disk. RELIANCE's 253 minute sessions now fall within 09:15–15:30. **Any earlier result computed on long history (walk-forward, outcome validation, the failed outcome-model candidate) used shifted sessions and must be rerun.** Recent scans used recent, correctly labelled candles.

### 2026-10-24: Adaptations from external repositories

Reviewed five public intraday projects:

- `arshadakl/intraday-trading-bot`: NSE, Angel One; gap plus 3-minute opening-range breakout, VWAP+RSI, pivots, cost calculator.
- `pushpendughosh/Stock-market-forecasting`: LSTM/RF cross-sectional ranking, published paper.
- `Yvaine-Zhang/Models-for-Intraday-Trading-Volume-Prediction`: U-shape, Kalman and ARIMA volume forecasts.
- `wassupansh/Intraday-Trading-Navigating-the-Market-Waves`: single-stock notebook with FinBERT sentiment.
- `GeorgeHigham/Intraday-Trading`: toy S&P strategies with no costs.

Adopted now:

- Classic floor pivots, added to the technical summary and shown in the symbol view. The technical summary was previously API-only.
- An optional `PAPER_ONE_ENTRY_PER_SYMBOL_PER_DAY` re-entry block, applied to every entry path.

Also fixed a latent lookahead in `add_intraday_patterns`: the opening range used a whole-hour max/min, so early bars saw later opening-hour prices. It now uses running extremes, and a regression test fails on the old code. No live path called it.

Queued as hypotheses (see Open work): the volume forecast, the Nifty gap/opening-range strategy and the cross-sectional ranking model.

Rejected:

- arshadakl's "70% win rate" claim, which has no evidence behind it.
- Their cost rates: our NSE rate (0.00297%) and GST base are more current.
- wassupansh's 94% F1, which is likely leakage; FinBERT was skipped because the decision is OpenRouter only.
- GeorgeHigham's strategies.

### 2026-10-24: Documentation and repository cleanup

Made this file the project tracker. Retired `TASKS.md`, `TASKS_INTEGRATION.md`, `INSTITUTIONAL_ROADMAP.md` and `ML_REDESIGN_SUMMARY.md` (their open items are merged above, their history below). Removed the dead root `paths.py` (duplicate of `src/trading_agent/paths.py`), the stale `QUICKSTART.py`, one-off `artifacts/` repair scripts and screenshots, and committed runtime caches. Generated output (`artifacts/`, `optimization_results/`, data caches, controls and drift state) is now gitignored. CI also runs on `unified-platform`.

### 2026-10-23: Unified platform (branch `unified-platform`)

User decisions: merge both UIs; OpenRouter is the only LLM provider; add risk controls, an execution layer, ML drift monitoring, performance analytics and volume-based recommendations; deliver in phased commits.

- **Phase 1** (`4395c0d`): restored regressed features: WAL pragmas, background candle writer, OpenRouter client with throttling (sentiment 429 fix), `validate_setup.py`, token lookup, Williams %R/CCI/DI, doji, Groww technical summary, backtest `--selftest`, walk-forward `--all`, session-feature causality checks.
- **Phase 2** (`e2d0b46`): restored the classic React dashboard from `gemini-old-interface` alongside the research workspace, backed by `api/routers/legacy_ui.py` using the research scan as the single source of truth.
- **MUI** (`40e132d`): Material UI theme, shared primitives, navigation bar and symbol picker.
- **Phase 3** (`5ccc16e`, `ebf5a76`): risk controls, order router, costs, reconciliation, `orders` table (migration 003), performance analytics, volume insights, drift monitoring, and the Risk & execution and Analytics pages.

Why: repeated refactors had silently dropped working features. Lesson: keep a feature map (above) and a regression test per restored feature.

Behaviour changes: manual paper entries now obey the kill switch, 15:00 cutoff and daily trade cap; classic-dashboard trades go through the order router with slippage.

### 2026-10-22: Image build and deployment

PyPI read timeouts fixed with longer pip timeouts, retries and a BuildKit cache. API, frontend, ticker and scheduler were redeployed with existing mounts. Kite rejected the WebSocket handshake (HTTP 403) until a valid session was supplied; live-data readiness was not established.

### 2026-10-21: Gemini repair and institutional roadmap

Scheduler/ticker signal handling and heartbeats fixed; readiness inventory made background and cached (47 s → 0.34 s). Paper entries gained single-position, realized daily-loss and drawdown limits inside the serialized SQLite transaction; scan allocations bind to a ledger/session/risk fingerprint. Opt-in automatic research scans, operations overview and Strategy Lab profile comparisons were added. 419 tests passed. The roadmap's remaining execution, account-truth, data-truth, exposure and security gates are carried in [READINESS.md](READINESS.md#gates-before-institutional-or-unattended-live-use).

### 2026-10-20: Research restoration and outcome-model lifecycle

Restored the original research layout and nine-feature classifier contract (commit `5dcea32`), full-watchlist scans, cached LLM sentiment and the corrected rule-only walk-forward optimizer. Added fixed-rule trade-outcome validation and a separate BUY/SELL outcome-model lifecycle with durable test reservations and prospective paper gates. The first candidate (`9b5e7dc3…`, five stocks) **failed** its historical gate and was not promoted. A RELIANCE minute walk-forward was negative after 5 bps costs. Lesson: report abstention and failure honestly; never invent win rates.

The older ML v2 redesign (walk-forward folds, 19 session/regime features, cost-aware labels, confidence thresholds) reported large backtest gains that were never independently revalidated; its summary document is retired and remains in Git history.

### 2026-10-19: Paper operating-path hardening and contract audit

Immutable Parquet batches, exchange-aware timestamps, persisted paper accounting, serialized entry caps and a versioned console. The audit fixed flat `.env` parsing, a `DEBUG=release` import failure, broken Docker entry points, schema disagreements between ORM/bootstrap/migration, scheduler wrapper types, backtests reading future candles, and lost stop/target values. Decisions: SQLite only until direct consumers migrate; `generate_from_data` for time-bounded research; live Docker services as opt-in profiles; mock LLM default in code; `data/.access_token` as the shared token path; one pytest configuration in `pyproject.toml`.

### 2026-10-18: Installable package, Docker image, integration layer

Runtime moved under `src/trading_agent` with console entry points; multi-stage Docker build. Portfolio exposure, position monitoring and opportunity exclusions implemented against integration tests. Lessons: exercise entry points after packaging; keep persisted schema and runtime model identical; compatibility belongs in aliases, not parallel tables.

### 2026-10-06: SQLite lock recovery

The live ticker added retries and connection recreation after lock failures. Lesson: WAL does not make SQLite a high-concurrency database; coordinate writers and keep transactions short.

### 2026-10-05: Edge diagnostics followed strategy changes

Lesson: research tools must import the same primitives as live decision paths or declare the difference.

### 2024-08-15: Exit research

A fixed take-profit capped winners in the studied sample. Point-in-time evidence only; rerun before relying on it. Lesson: exit rules can dominate entry tweaks, and backtest/live exit semantics must match.

## Operational heuristics

When diagnosing a problem, check in this order:

1. `python -m trading_agent.scripts.validate_setup --skip-llm`;
2. `GET /api/v1/health`, then `/api/v1/ready`, then `/api/v1/controls/status`;
3. `trading-agent storage-stats` and the relevant Parquet symbol;
4. `data/.access_token` freshness for Kite work;
5. process/container logs and `data/controls/audit-YYYY-MM-DD.jsonl`;
6. duplicate writers and SQLite lock state;
7. `/api/v1/analytics/drift` and model metadata for ML work.

An empty dashboard on a fresh install is normal: there are no candles or positions yet. A halted kill switch or an entry after 15:00 explains most rejected entries.
