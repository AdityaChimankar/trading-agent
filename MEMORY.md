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

Last updated: 2026-10-24 (ranking lab: 60 cross-sectional variants; one-year data, closing-auction fixes, out-of-sample ML, ranker paper trial). Active branch: `unified-platform` (from `gemini`), not yet pushed or merged.

The application is a single intraday NSE workbench: Kite data pipeline, rule/ML/LLM analysis, classic and research dashboards, a persistent paper book, intraday risk controls, a guarded order router, performance analytics, volume-based recommendations and ML drift monitoring.

**Verified** (2026-10-24, after the second trial arm): 542 Python tests passed (`PYTHONPATH=src`), repo-wide Ruff passed, the frontend type-check and production build passed, and both offline browser smokes passed. An earlier Playwright pass on a copy of the real 2026-10-21 scan and stored candles covered all eight dashboard routes with no console errors, failed API calls or mobile overflow.

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
| Data | `trading-agent data-check`: per-symbol coverage against the store-wide session calendar (up to date / behind / holes / empty / not listed by Kite), store staleness against the last closed weekday, Kite token, ticker heartbeat, trial arms; exit 1 on gaps. `backfill --universe all` fills them | `ingest/coverage.py`, `cli` |
| Research | Ranking lab: 60 pre-registered cross-sectional variants (fixed-sign gap/reversal/opening-drive ranks; RF/logit/HGB walk-forward; 09:15 or 09:45 entry; 10:15/12:00/15:15 exit; k 5/10/20); validation-only selection with a Bonferroni t over 60, frozen before holdout; rolling choose-best | `research/ranking_lab.py`, `trading-agent optimize ranking-lab` |
| Research | Ranker paper trial, two arms (`rf`, `hgb`), each with its own hashed frozen spec (config, universe, first session), pick after 09:15 bar (deadline 09:45), entry at the first bar after the pick, 15:15 exit, random book, missed sessions recorded, verdict fixed on the first 60 settled sessions | `research/ranker_trial.py`, `trading-agent optimize ranker-trial`, scheduler `ranker_trial_pick`/`ranker_trial_settle`, `/api/v1/analytics/cross-sectional/trial`, Analytics panel |
| Research | Cross-sectional ranking (Ghosh et al. 2021) walk-forward vs random picks, 15:15 MIS exit, >= INR 10 crore liquidity filter, research gate, long/short list only when passed | `research/cross_sectional.py`, `trading-agent optimize cross-sectional`, `/api/v1/analytics/cross-sectional`, Analytics panel |
| Research | Strategy search: 2,508 entry x bar-size x exit candidates; selection on development/validation only (Bonferroni clustered t, session bootstrap, symbol stability, always-long/short/random baselines, 10+2x2 bps) frozen to a hashed run file before holdout; ML predicted walk-forward (expanding, refit every 20 sessions after 60), never on its own training sessions; rolling walk-forward over exit policies and all candidates including ML; immutable runs with git revision, versions and data fingerprints | `research/strategy_search.py`, `trading-agent optimize strategy-search` |
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
| P0 | Run the ranker paper trial | started 2026-10-24: first session 2026-10-26, spec `da100b7c01949a78...` in `data/research/ranker_trial/spec.json`, frozen at `449418e`. Needs the scheduler from this branch (the main checkout is on `rework`, which lacks the trial) running each session with a valid Kite token, then the verdict on the first 60 settled sessions (mean net > 0, t >= 2, above random) recorded here. Every historical window has been inspected, so only these sessions are clean evidence |
| P0 | Run the `hgb` trial arm | started 2026-10-24 beside `rf`, first session 2026-10-26, spec `0111c28619f97074...` in `data/research/ranker_trial_hgb/`, frozen at `68aaee0`. Verdict on its first 60 settled sessions; t >= 2.0 keeps the family-wise error across both arms below 5% |
| P1 | Cross-sectional ranking on longer history | daily or intraday history long enough for `--max-lag 240`; point-in-time Nifty 500 membership to remove survivorship bias |
| P1 | Align legacy database indexes | `alembic check` on the live `data/trading.db` reports no drift (index names, nullability on `ml_models`, `opportunities`, `candles` and others) |
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
| The API creates missing tables at startup, before migrations | after a schema change, run `docker compose run --rm tools alembic upgrade head` before starting the new image; otherwise a pre-created table blocks the migration |
| NSE closing auction (from 2026-08-03): ~40% of the universe has no continuous-trading minute bars after 15:14 | ranker features use the 15:15 price; the bracket replay force-exits on the session's last bar when it ends at or after 15:15. Any other code that needs a 15:25+ bar must be checked |
| `STORAGE_PARQUET_ROOT=data/parquet` in `.env` is relative to the working directory | run data jobs from the main checkout, or set an absolute path; a worktree otherwise writes its own empty store |
| `.test-tmp*` and `.pytest_cache` in the worktree have locked ACLs | gitignored; delete with elevated permissions if needed |

## Decision log

Newest first. Each entry records what changed and why; verification counts were true on that date only.

### 2026-10-24: Data freshness check across all stored symbols

**Request.** How to check that all data is up to date, across the ~2,270 NSE stocks.

**Change.** `trading-agent data-check` and `ingest/coverage.py` (see the User Guide). `HistoricalFetcher.stored_days` now uses the same code, so the check and `backfill --missing-only` agree on what is missing. `backfill` and `data-check` both take `--universe watchlist|nifty500|all`.

**First full run** (2026-10-24, 2,312 stored minute symbols, last 30 days): 494 up to date (the Nifty 500 topped up earlier), **1,794 behind** (stopped around 2026-10-01), and 24 not listed by Kite (for example ICICIPRULI, MOREPENLAB, ALUFLUOR). The store's newest session, 2026-10-23, matches the last closed weekday. The token was valid and both trial arms were ready for 2026-10-26. The full run takes about 5 minutes. A 30-day fill of the behind symbols, `backfill --universe all --interval minute --days 30 --missing-only`, was started the same day. **Thin-stock false alarms, fixed.** After that fill, 1,616 symbols were up to date, while 253 showed as behind and 419 had holes. Sampling showed these are thinly traded stocks with every bar Kite has (for example ADL at a median 49 bars a session, first trade after 10:30), not gaps. Each successful fetch now records its window in `data/ingest_coverage/<interval>/<SYMBOL>.json`. A session counts as covered when its bars are complete or it was fully fetched, in both `data-check` and `backfill --missing-only`, so thin stocks are not re-requested daily. The top-up was re-run to record those windows: 1,934 up to date, 0 behind, and 354 with holes, all on 2026-09-23/24. That was a window mismatch: `data-check` counts 30 days back from the last closed session, while `backfill` counted from the moment it ran (Saturday evening). Both now use `coverage.window_start`, and `--missing-only` starts at that session boundary. A full `data-check --universe all` now takes ~15 minutes, because each top-up adds one small Parquet file per symbol; compacting the store is the remedy. The README gained a "Market data and backfill" section: commands, `--universe` and `--missing-only` semantics, measured timings (about 1 symbol a second for a 30-day top-up, about 20 s per symbol-year of minute data), the `data-check` loop and pitfalls; the daily schedule gained an after-close `data-check` row.

### 2026-10-24: Docker images rebuilt

**Images.** Built from `4494f3f`: `trading-agent-app:unified-platform` and `trading-agent-frontend:unified-platform`, also tagged `latest`. The previous `latest` images are kept as `pre-unified-20261024`. The app image includes the ranker trial (both arms), the ranking lab and the scheduler's trial jobs.

**Smoke fixes.** `docker-compose.smoke.yml` was failing for two reasons unrelated to the build:

- Its synthetic paper positions hit the intraday session gate on a weekend (`409 Market closed`). The smoke `api` and `smoke` services now set `PAPER_ENFORCE_SESSION=false`, as the pytest suite does.
- The browser check opened the site root, which is the overview since the UI merge. It now opens `/#/research`.

The smoke stack then passed: migrations, Parquet replay and compaction, nginx proxy, authentication, research panels, and the browser paper lifecycle with persistence.

**Live stack switched to the new images.** The running containers (compose project `trading-agent`, project directory = main checkout, file = this worktree's `docker-compose.yml`) were still on the old images. Recreating them left the API unhealthy: `data/trading.db` was at revision `001` and the code needs `003`. Steps taken:

1. Stopped the writers.
2. Backed up with the SQLite backup API to `data/backups/pre-schema-003-20261024T100259Z.db` (integrity ok).
3. Ran `alembic upgrade head`. Revision `002` applied; `003` failed because the briefly started new API had already created an empty `orders` table at startup (`database_tables_created`).
4. Checked that table against the migration (same columns and indexes, 0 rows), dropped it, and re-ran: the database is at `003`.
5. Started the stack: API ready, frontend 200, both trial arms visible (`rf` `da100b7c...`, `hgb` `0111c286...`), and the scheduler registered `ranker_trial_pick_cycle` and `ranker_trial_settle_cycle`.

`alembic check` still reports legacy index-name and nullability drift in this older database (a fresh database is clean); a migration for it is open work.

**Rebuilt again from `adfa557`** (adds `data-check`, `backfill --universe`): app `dcfcebec8d50`, tagged `unified-platform` and `latest`. The previous images are kept as `unified-4494f3f`. There was no new migration (head still `003`), so the stack was recreated directly: API ready, frontend 200, scheduler trial jobs registered, and `data-check --universe nifty500` run inside the container (494 up to date, 7 not listed by Kite).

### 2026-10-24: Second paper-trial arm (gradient boosting)

**Change.**

- `RankConfig.model`: `rf` (the default, unchanged) or `hgb`. `hgb` is gradient boosting on per-session percentile ranks of the lag features, with the ranking lab's settings.
- The trial gained arms, each with its own directory, spec and records. `pick_all` builds the morning table once for arms that need the same data and fetches from Kite once; `settle_all` fetches once.
- The `rf` spec frozen earlier today predates the `model` field and loads unchanged (default `rf`, same hash). A test covers this.
- The API takes `?arm=`; the Analytics page shows one panel per arm; the CLI takes `--arm`.

**Family-wise bar.** The existing t ≥ 2.0 already covers two arms: the Bonferroni one-sided bar at 5% across two arms is 1.96. So the frozen `rf` spec did not need to change.

**Dry run** (2026-10-23, stored data, scratch folder): `pick_all` took 154 s for both arms, and the `rf` book was identical to the single-arm dry run. Settlement: `rf` +25.4 bps, `hgb` +45.9 bps; random books −15.4 bps. One day of plumbing, not evidence.

### 2026-10-24: Ranking lab (60 cross-sectional variants)

**Request.** Try more methods and variations for a better win rate. Variants are ranked by net return after costs, because the strategy search showed that high win rates are easy to buy at negative expectancy.

**Method.** `research/ranking_lab.py` (see the User Guide). Run `20261024T073205Z-ef51dc21`: 495 symbols, 192 common test days, validation 2026-01-13 to 2026-07-03 (115 days), holdout 2026-07-06 to 2026-10-23 (77 days), 15 bps per position, Bonferroni t ≥ 3.14 over 60 variants.

**Validation.** 18 of 60 variants were positive net; 38 of 60 positive gross. **None passed the Bonferroni bar.**

| Variant | Net bps/day | t | Day win | Position win |
|---|---|---|---|---|
| `hgb_lags` 09:15–15:15, k10 (validation best) | +16.8 | 2.73 | 56.5% | 55.2% |
| `hgb_lags` k5 | +15.5 | 1.97 | 53.9% | 54.1% |
| `gap_reversal` 09:15–15:15, k5 | +12.8 | 1.12 | 57.4% | 52.2% |
| `hgb_lags` k20 (highest day win rate) | +10.2 | 2.44 | 60.9% | 53.3% |
| `logit_lags` k10 | +10.0 | 1.42 | 52.2% | 53.4% |

The opening-drive (09:45 entry) variants and the momentum signs did not make the top 15.

**Holdout** (frozen set only):

- `hgb_lags` k10: **+13.3 bps/day, t 2.51, day win 66.2%, position win 54.2%**.
- Reference `rf_lags` k10 (the trial config): +14.1 bps/day, t 2.20, day win 59.7%.

**Rolling choose-best** over all test days: +2.9 bps/day, t 0.49. The chosen variant flipped between folds (+26, +0.2, −22.6, −11.0, +20.8, +4.7, +1.2 bps), so picking a variant from the data is not yet reliable.

**Reading.**

- Lag-feature models (gradient boosting, then logit and random forest) and overnight-gap reversal are the consistent winners. Gradient boosting on per-session ranked lags has the best validation result and the best holdout win rate.
- It did not clear the multiple-testing bar.
- The holdout coincides with the post-closing-auction period, where the reference RF did as well. Part of the holdout strength is therefore that period, not the method.

**Decision.** Nothing changes in live trading or in the frozen trial. The candidate for a second prospective arm is `hgb_lags` k10; its verdict would need a family-wise bar across both arms.

### 2026-10-24: One year of data, closing-auction fixes, out-of-sample ML, ranker paper trial

**Data.** The minute store for `symbols_nifty500_plus.txt` was topped up to 2026-10-23. 495 symbols are stored, most from 2025-09-29, and 90% have at least 255 sessions. Seven symbols are not in the instrument list: BAGMANE, BIRET, DUMMYHEG, EMBASSY, HFCL, ICICIPRULI and STLTECH. `trading-agent backfill --days N --missing-only` now requests only sessions that are missing or incomplete. It compares each symbol with the calendar seen across the store; the first version looked only at the stored first and last bar and skipped interior holes left behind partial live-ticker days.

**Closing auction.** From 2026-08-03 about 40% of the universe (largely F&O names) has no continuous-trading minute bars after 15:14. Two consequences:

- The ranker treated those stocks as having no close and silently dropped them, so after August it ranked a skewed universe. Features now use the 15:15 price for every stock.
- The bracket replay dropped any trade still open at 15:14 instead of exiting it. It now force-exits on the session's last bar when that bar ends at or after 15:15, in the scalar and vector paths (parity-tested).

**ML win rate (68% to 38%).** Diagnosed as in-sample scoring, not a regime change. `score_ml` fitted on development and then traded development.

- Real data (15-minute bars, 120-minute horizon, reward/risk 1.5): the base win rate after costs is about 30% in both periods. The model scored 63% in-sample on development, 45% when development was predicted walk-forward, and 39% on validation. AUC was 0.61 in-sample and 0.56–0.57 on validation.
- Repair: every ML session is now predicted by models fitted only on earlier sessions (expanding window, refit every 20 sessions, after 60 training-only sessions; blocks never straddle a period; holdout models are fitted only after freezing). ML joins the all-candidates walk-forward.
- Rerun `20261024T070312Z-550d476d` (264 sessions, holdout from 2026-08-07): ML scored 43.2% wins and −10.0 bps on development (out of sample), 42.7% and −12.7 bps on validation, so the gap is gone. 0 of 2,508 candidates were shortlisted and 0 were nominally positive. Walk-forward over all candidates was −6.8 bps on 350 trades (CI −18.6 to +4.6); over the rule's exit policies, −13.6 bps. Conclusion unchanged: no single-stock intraday edge after costs.

**Ranker, corrected features, full year** (frozen defaults, 181 test days; every window has been seen before, so this is not evidence): +4.6 bps/day, t 1.1, hit rate 52.5%, against −14.8 for random picks. The research gate fails on t. Split at the auction change: +1.8 bps/day (t 0.35) over 135 days before 2026-08-03 and +13.0 (t 1.74) over 46 days after. The earlier +11.2 holdout figure came mostly from the recent weeks.

**Paper trial built** (`research/ranker_trial.py`):

- **Spec:** frozen and hashed (config, universe, first session); a start in the past is refused.
- **Pick:** after the 09:15 bar, deadline 09:45. It fetches today's bars for the liquid names, blanks everything after the open, trains on the preceding 60 labelled sessions, and records the top and bottom 10 once.
- **Settle:** at 15:50. Entry is the open of the first bar after the pick, exit the 15:15 price, net of 15 bps. The at-open return and a random book from the same eligible names are recorded beside it.
- **Missed sessions** are recorded, never filled in.
- **Verdict:** fixed on the first 60 settled sessions (mean > 0, t ≥ 2, above random).
- **Wiring:** scheduler jobs `ranker_trial_pick` (09:16:30) and `ranker_trial_settle` (15:50); `GET /api/v1/analytics/cross-sectional/trial`; Analytics panel; `trading-agent optimize ranker-trial`.
- **Dry run** on 2026-10-23 from stored data, in a scratch folder: pick 186 s with 481 eligible names; settle 71 s, +25.4 bps. Plumbing only.

**Also:** removed the f-string lint errors in `manual_login.py`; recorded the relative `STORAGE_PARQUET_ROOT` pitfall under Known limitations.

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
