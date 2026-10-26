# Trading Agent User Guide

This guide covers a first local run, Docker operation, the daily Kite workflow, research commands, and common recovery steps. For the shorter path, start with [README.md](README.md).

> Safety: orders run in paper mode unless you set both `EXECUTION_MODE=live` and `EXECUTION_LIVE_CONFIRMATION=I-ACCEPT-REAL-MONEY-RISK`. Live orders have not been verified against a real account.

Read [READINESS.md](READINESS.md) before unattended use; live-session, accounting and security gates are still open. Project status and open work are tracked in [MEMORY.md](MEMORY.md).

## Dashboard pages

The top bar has a tab per page and a searchable symbol picker that sets the symbol for every page.

| Tab | Use it to |
|---|---|
| Dashboard | Review the watchlist (bias, blocked/stale flags, contradictions with open positions), the bullish/bearish Top Opportunity, symbol detail, wallet, Position Monitor and the morning digest. Open a paper trade from a fresh, unblocked plan |
| Screener | Sectors trending today, preset trading screens and the intraday screener (below); **Update technicals** recomputes them |
| Research | Run the full-watchlist scan and the Find → Inspect → Validate → Paper review workflow (below) |
| Paper operations | Record, monitor and close paper positions manually |
| Risk & execution | Engage or release the kill switch, square off everything, check session status, inspect orders and reconciliation, preview charges, read the audit trail |
| Analytics | Read net performance, the volume board and the ML drift monitor |
| Signal Pipeline | See the latest rule, LLM and ML decision per symbol side by side |
| Transactions | Review the transaction history and strategy status |
| News | Read headlines matched to the watchlist with cached sentiment |

The symbol view's **Technical summary** shows a Groww-style vote across RSI, MACD, Stochastic, Williams %R, CCI, ADX/DI and moving averages. Next to it are classic floor pivots for today, calculated from the previous IST session's high, low and close: pivot = (H+L+C)/3, R1 = 2P−L, S1 = 2P−H, R2/S2 = P ± (H−L), R3 = H + 2(P−L), S3 = L − 2(H−P). The panel also shows the nearest pivot above and below the price. Both are context only. They never change the rule signal or the scan's room-to-target, and pivots need a completed previous session in stored candles (`GET /api/symbols/{symbol}/technicals`).

### Market screener

The **Screener** tab rebuilds four sections of Groww's Explore page from stored minute candles and live Kite quotes:

- **Top movers today**: gainers, losers and volume shockers within an index (NIFTY100 by default).
- **Sectors trending today**: for each NSE industry, gainers and losers, plus the equal-weighted mean and median 1D change. Click a sector to filter the screener to it.
- **Trading screens**: resistance breakouts (above the previous session's R1), near R1 (within 1% below), support breakdowns (below S1), MACD positive (line above zero: what Groww's "MACD above signal line" screen actually filters), MACD above or below its signal line, RSI ≥ 70 and ≤ 30, near the 52-week high (within 5%) and volume shockers (today's volume at least 2x the 1-week daily average). Each chip shows how many stocks match.
- **Intraday screener**: combine price change (last 5 minutes, 15 minutes, 1 hour or today), RSI band, MACD side, 52-week proximity, R1–R3 breakout or nearness, index and sector. Sort by turnover, change, volume against the 1-week average, RSI or price.

The conventions follow Groww's. RSI(14), MACD(12,26,9), pivots and the 52-week range use completed daily sessions. The 1-week average volume covers the sessions of the previous 7 calendar days. The previous close is the exchange's official close taken from the quote. The live-bar RSI is shown when you **Check** a row. Turnover is LTP × today's volume. Market cap is not available.

Nothing is computed when a page loads. **Update technicals** (or `trading-agent screener-scan`) runs in the background. It first fetches live Kite quotes for the whole universe (about 5 calls for 2,300 symbols, one per second), then lays them over each symbol's stored minute history in worker processes, and saves `data/research/screener.json`. A full scan took about 2 minutes on 2026-10-26. Without a valid Kite token, the scan uses stored candles only, and the header says quotes were unavailable. In that case, names the ticker does not stream show as lagging. `--no-quotes` (or `{"quotes": false}`) skips the calls. **Indexed only** scans just the roughly 750 names in the sector map. During the session, `trading-agent screener-scan --watch 5` repeats the scan every 5 minutes until 15:30.

The header shows the technicals check: current rows, live-quoted rows, stale rows (an older session) and lagging rows (more than 15 minutes behind the newest price, counted to 15:30 at the latest, usually a ticker gap), plus rows without enough history for RSI (15 sessions), MACD (35 sessions) or a full 52 weeks. Stale and lagging rows are left out of screens and sector breadth. **Check** on a row recomputes that stock now and lists what changed since the saved scan.

Below the screen chips, a **track record** shows how each screen did historically. `trading-agent optimize screen-outcomes [--decision HH:MM] [--cost-bps X]` re-runs it over the Nifty 500-plus list in about a minute and a half, and the result is saved to `data/research/screen_outcomes.json`. Each screen is evaluated at the decision time (default 10:15) with only what was known then. Flagged names are entered at the 10:15 bar's open on the screen's side and exited before 15:15. The table shows the net result after costs and the edge against the whole liquid universe (a random pick), for a development period and a later holdout. A screen counts as an `edge candidate` only with positive net and edge in both periods and a holdout t of at least 2.81 (corrected for testing 10 screens). On 2026-10-26, no screen passed, at either 10:15 or 09:30, so treat the screens as watchlists, not signals.

Sectors and index filters need the sector map. Run `trading-agent sector-map` to download NSE's index lists (Nifty Total Market, which is Nifty 500 plus Microcap 250, with 22 industries) into `data/cache/sector_map.csv`. A copy is committed, so a fresh checkout works offline. Unlisted symbols stay unclassified.

The Dashboard digest is built in the background from the top 30 scan names and cached for 15 minutes. Run a research scan first; without one, the classic pages show empty states rather than invented rankings.

## Intraday risk controls and execution

All entry paths (Dashboard, Paper operations, `POST /api/v1/execution/enter`) use the same order router. Before any order is sent it checks, in order:

1. kill switch: **Risk & execution → Engage kill switch** writes `data/controls/kill_switch.json`, which survives restarts. An unreadable file blocks entries; **Resume trading** clears it;
2. session: no entries before 09:15 IST, after `PAPER_ENTRY_CUTOFF` (15:00) or at weekends. `PAPER_ENFORCE_SESSION=false` disables only these time gates;
3. the daily trade cap (`PAPER_MAX_TRADES_PER_DAY`), and, when `PAPER_ONE_ENTRY_PER_SYMBOL_PER_DAY=true`, no second entry in a symbol already traded today (no re-entry after its stop or target);
4. the entry price: the most recently ended stored bar, 1-minute or the live interval, if it ended no more than `EXECUTION_MAX_QUOTE_AGE_SECONDS` (900) ago, is the fresh price. Risk is measured from the worse of it and the plan price, and the paper fill starts from it. A fresh price at or through the stop or the target refuses the entry. Live entries need a fresh price;
5. the participation cap: quantity × price must not exceed `EXECUTION_MAX_PARTICIPATION_PCT` of the stock's average daily traded value;
6. the paper-ledger limits inside one SQLite `BEGIN IMMEDIATE` transaction: stop distance (`PAPER_MIN_STOP_DISTANCE_PCT`), reward:risk at the entry price (`PAPER_MIN_REWARD_RISK`), duplicates, positions, exposure, stop risk, single-name cap, realized daily loss, worst-case daily loss and drawdown.

The router reserves the position, sends the order and then rewrites the position at the fill price. If the broker rejects the order, the reservation is deleted and the API returns 502. Exits always remain available.

The scheduler squares off all open positions at `PAPER_SQUARE_OFF_TIME` (15:15 IST). **Square off all positions now** does the same on demand. Every open, close, fill, rejection, halt and resume is appended to `data/controls/audit-YYYY-MM-DD.jsonl` and shown under **Audit trail**.

| Mode | Fills |
|---|---|
| Paper (default) | Reference price moved `EXECUTION_SLIPPAGE_BPS` against you |
| Live | Kite MARKET order, product `EXECUTION_PRODUCT` (MIS); the router polls order history for the average fill price |

**Reconciliation → Run check** lists unconfirmed orders and fill mismatches, and in live mode compares the ledger's net quantity per symbol with Kite positions.

The **Charges calculator** (`GET /api/v1/execution/costs`) shows entry charges, round-trip charges at the same price, and the break-even move. The rates follow Zerodha's published intraday schedule; check them against zerodha.com/charges before relying on them.

Before switching to live: build evidence in paper mode, confirm the charge rates, and do one small supervised round trip. Then check reconciliation and the audit log.

## Analytics, volume and drift

**Performance** uses closed positions. Net P&L subtracts the charges of filled router orders, or modelled round-trip charges (marked `*`) for manual entries. It reports win rate, expectancy, profit factor, average R, max drawdown and Sharpe/Sortino, all from daily net returns annualised over 252 days. Results are attributed by strategy and symbol. Sharpe and Sortino need at least two trading days, and below 20 trading days the page warns that the ratios are not meaningful.

The **volume board** takes the strongest directional scan names and compares today's cumulative volume with the same time of day over the previous 20 sessions (RVOL). It then gives a volume call:

- `BUY_CONFIRMED` / `SELL_CONFIRMED`: RVOL ≥ 1.5, price on the matching side of session VWAP and the last five bars moving that way;
- `WATCH`: heavy volume without a clear side;
- `AVOID`: RVOL < 0.7 or average daily traded value under ₹1 crore;
- `NEUTRAL` otherwise.

The board also shows liquidity tier, the maximum quantity under the participation cap, and whether the volume call confirms or contradicts the scan. With fewer than five prior sessions, RVOL is marked unreliable. These are rules of thumb, not a backtested signal. A single symbol is available at `GET /api/v1/analytics/volume/{symbol}`.

**Volume forecast.** Each volume-board row also forecasts how much volume is still to trade today. It uses the stock's intraday U-shape: the median share of a session's volume normally traded by this minute, taken over the previous 20 sessions. That profile is blended with today's own pace, and the weight on today's pace grows as the day fills in. The router refuses entries above `EXECUTION_MAX_REMAINING_VOLUME_PCT` (5%) of that forecast. Check how well it works for a symbol with `GET /api/v1/analytics/volume/{symbol}/forecast-check`. That endpoint replays every complete session out of sample and reports the mean absolute percentage error at 10:15, 12:00 and 14:00. It compares the error against two baselines: the flat 20-session average, and today's straight-line pace.

On 15 large NSE symbols (about 3,000 sessions, checked on 2026-10-24), the median errors were 27% / 19% / 12% against 37% / 20% / 17% for the better baseline. The weighting was chosen on that same data, so treat it as in-sample.

**Cross-sectional ranking (research).** This is adapted from Ghosh, Neufeld & Sahoo (2021). Every session, a random forest ranks the universe (default `symbols_nifty500_plus.txt`) by the probability that a stock's open-to-close return beats the day's median. The strategy buys the top `k` (default 10) at the open, shorts the bottom `k`, and exits at 15:15, the MIS square-off time, not the 15:30 close. Only stocks averaging at least ₹10 crore a day in traded value over the previous 20 sessions are ranked (`--min-value-cr`). Features are lagged open-to-close, close-to-close and overnight returns (lags 1–10), all known at the open. The walk-forward retrains every 20 sessions on the previous 60, and results are compared after `cost_bps` (15 bps per round trip, including slippage) with random picks. These defaults were chosen on the validation half of a walk-forward; see MEMORY.md.

The research gate needs at least 60 test days, a positive net mean, a t-stat of at least 2, and a result better than random picks. Only when it passes does the report publish a long/short list for the latest session. The list is not an order and does not feed the scanner.

Run it from **Analytics → Run evaluation** or `trading-agent optimize cross-sectional`. Daily bars come from stored intraday candles, and sessions without a 09:15 bar or a 15:15 price are skipped. All features use the 15:15 price as the day's close: since NSE's closing auction (3 August 2026) about 40% of stocks have no minute bars after 15:14. The paper's longest lag is 240 days; `--max-lag 10` is the default because most stored history is shorter. Survivorship bias (today's watchlist omits delisted stocks) makes results look better than reality.

**Decision outcomes (research).** `trading-agent optimize decision-outcomes` (module `research/decision_outcomes.py`, adapted from TradingAgents' settlement step) scores every BUY/SELL row in the `signals` table: rule, ML, LLM, hybrid and shadow strategies alike. It answers whether the decisions made money, which the older `scripts/llm_eval.py` agreement check never measured. That script also reads an `llm_signals` table that no longer exists.

- **Pricing:** each signal enters at the open of the first bar at or after it, and exits after `--horizon` minutes (default 60) or at 15:15, net of `--cost-bps` (default 14).
- **Alpha:** when NIFTY50 candles are stored, the index return over the same bars is subtracted.
- **Grouping:** results are grouped by strategy, by strategy and action, and by confidence band (<0.60, 0.60–0.75, ≥0.75), with win rates and a t-stat on per-session sums.
- **Not losses:** HOLD, pending (the window has not finished), unpriced (no bars) and after-cutoff signals are counted but never scored as losses.

The scheduler scores the last 30 days at 15:55 on weekdays. See **Analytics → Decision outcomes**, or `GET /api/v1/analytics/decision-outcomes` (`POST …/run` re-scores). With `LLM_PROVIDER=mock` (the code default), stored `llm` signals come from the mock client, and the report says so.

**Trade replay (research).** `trading-agent optimize trade-replay` (module `research/trade_replay.py`, after the day-trader-skill pack's Time Machine) asks how the rest of today could treat one bracket before you size it. On the Dashboard, **Position Sizing → Replay this plan** runs it for the shown plan; the API is `POST /api/v1/analytics/trade-replay`.

- **Bars:** fixed, non-overlapping bars anchored at 09:15 IST (open first, high max, low min, close last). 1-minute bars are used when the 1-minute history has at least 90% as many usable sessions as the 5-minute one, so exits are checked minute by minute; otherwise 5-minute bars (`--bar-minutes` overrides). A session needs its 09:15 bar and at most 2% of its bars missing; a missing bar had no trade and is filled flat.
- **Simulation:** filtered historical simulation with a stationary block bootstrap. Each past bar is divided by its session's volatility and the time-of-day volatility profile, then rescaled to today's volatility (today's bars before the entry, blended with the median of the last 10 sessions). Each of 10,000 replays (seed 19, repeatable) copies runs of consecutive bars from random past sessions at the same time of day (±10 minutes). Run length is random, with a mean chosen per stock from the data (Politis–White, capped at 60 minutes), so trends and volatility bursts inside a run survive.
- **History:** the same percentage bracket entered at the same time on each of those real sessions. It is a base rate for the time of day, not for the signal.
- **Exits:** the same as the outcome research. A bar that opens through the stop exits at that open, one that opens through the target exits at the target, a bar touching both counts as the stop, and anything still open exits at 15:15.
- **Verdict:** weighs the target's R and costs. A 2R bracket with 14 bps of costs only has to hit the target first in about 1 of 3 replays that hit a side, plus the cost share.
- **Conclusion:** the replay is rerun on 200 resamples of the session pool to get a 90% range of net R and of the target-first share. It reports `conclusive negative` or `conclusive positive` only when the pool has 40+ sessions, both ranges clear zero and break-even on the same side, and three checks agree in sign: single-bar draws, unscaled bars, and the real-session history. Otherwise it reports `inconclusive` with the failed checks. A positive is a recent base rate for this bracket at this time of day, not evidence that the signal has an edge. The status is shown on the Dashboard; it never blocks an entry.
- **Entry time:** defaults to the end of the latest stored bar. After the last bar before 15:15 there is nothing left to replay, so after hours pass `--at` (CLI) or `entry_time` (API). At least 20 usable sessions are required.

News, earnings and halts are missing, and dependence longer than a block is lost. The odds are not a win probability, not a prediction and not evidence of an edge.

**LLM model tiers.** `LLM_QUICK_MODEL` serves the high-volume, low-stakes calls (headline sentiment and the setup check), and `LLM_DEEP_MODEL` serves LLM trading decisions. Both fall back to `LLM_MODEL`. This mirrors TradingAgents' quick/deep split, which keeps cost down without weakening the decision model.

**Ranking lab (research).** `trading-agent optimize ranking-lab` (module `research/ranking_lab.py`) tries 60 variants of the cross-sectional idea, fixed before the run:

- **Rule ranks with a sign fixed in advance** (nothing is fitted): overnight-gap reversal and momentum, previous-day and 5-day reversal, opening-drive (09:15–09:45) momentum and reversal, and drive times opening volume shock.
- **Walk-forward models:** random forest, logistic regression and gradient boosting, on the lag features alone or with gap, drive and volume shock.
- **Settings:** entry at 09:15 or 09:45; exit at 10:15, 12:00 or 15:15; books of the top and bottom 5, 10 or 20.

Every variant is a long/short book at 15 bps per position, on the same test days. Selection uses the first 60% of test days only: a positive mean and a daily t-stat above the Bonferroni bar for 60 variants (3.14). The shortlist, the validation-best and the trial's reference configuration are written to `runs/<id>/selection.json` before any holdout statistic is computed. A rolling choose-best (trailing 60 test days, apply the next 20) measures the selection procedure itself. Day and position win rates are reported, but variants are ranked by mean net return.

`--snapshots FILE` caches the per-session price snapshots, so later runs skip reading the minute store.

**Ranker paper trial (prospective).** Every historical window has been inspected, so the only clean evidence for the ranker is sessions that have not happened yet. `trading-agent optimize ranker-trial start` freezes the defaults, the universe and the first session (the next one; a start in the past is refused) in `data/research/ranker_trial/spec.json`, with a hash that is checked on every read. Each session then gets two records under `sessions/`, each written once:

1. **Pick** (scheduler, 09:16:30 IST, deadline 09:45). It fetches today's minute candles for the liquid names, trains on the preceding 60 labelled sessions and records the top and bottom 10 with the pick time. Only today's open is used; later bars are blanked.
2. **Settle** (scheduler, 15:50 IST). Each position enters at the open of the first minute bar after the pick and exits at the 15:15 price, net of 15 bps. The at-open return (the backtest's assumption) and a random-pick book from the same eligible names are recorded beside it. A trading session without a pick is recorded as **missed** and never filled in.

There are two arms, each with its own frozen spec and records: `rf` (the random forest; `data/research/ranker_trial/`) and `hgb` (gradient boosting on per-session ranked lag features, the ranking lab's validation-best; `data/research/ranker_trial_hgb/`). Start each with `--arm`. The scheduler's pick fetches and loads the morning's data once for both arms, and settlement fetches once.

The verdict is computed once per arm, on its first 60 settled sessions: mean net > 0, t ≥ 2 and above the random book. A t of 2.0 keeps the one-sided family-wise error across the two arms below 5%. Later sessions are shown but cannot change it. See **Analytics → Ranker paper trial**, `trading-agent optimize ranker-trial status`, or `GET /api/v1/analytics/cross-sectional/trial`. The trial needs a valid Kite token each morning and places no orders.

**Moving averages.** The chart shows MA20 and MA50, plus EMA9, EMA21 and session VWAP (VWAP restarts at each 09:15). Click EMA9 or EMA21 in the legend to show them. Strategy Lab has two moving-average profiles:

- `rule_ma`: the existing rule, keeping a BUY only while EMA9 > EMA21 and a SELL only while EMA9 < EMA21.
- `ema_cross`: EMA9 crossing EMA21 with ADX ≥ 20, on the matching side of session VWAP.

The live rule signal is unchanged. A test on 20 large NSE symbols (2026-10-24, newest 40% of the history, shared bracket policy) found no edge from moving averages on 1-minute or 5-minute bars:

- Before costs, every variant, including the unconditional baselines, averaged between −2.6 and +1.6 bps per trade.
- After 10 bps costs plus 2 bps slippage per fill, every variant lost 12–15 bps per trade.
- The filters cut trades by 70–90% and changed the per-trade result by about 1 bp.

Requiring price above both EMAs removed every rule signal, because the rule buys pullbacks (RSI < 45). That is why `rule_ma` uses EMA alignment only.

**Strategy search (research).** `trading-agent optimize strategy-search` (module `research/strategy_search.py`) scores every combination of the following, on 30 liquid stocks, with the shared bracket replay:

- **Entry type:** the rule, the rule with an EMA filter, trend pullback, breakout, EMA crossover, opening range, VWAP and RSI mean reversion, VWAP trend, and a gradient-boosting ML filter.
- **Bar size:** 5, 15 or 30 minutes.
- **Exit policy:** stop multiple, reward/risk and maximum bars.

Sessions are split by date into development (60%), validation (20%) and holdout (20%). A run has two phases:

1. **Selection** sees development and validation only, at conservative costs (10 bps plus 2 bps slippage per fill). A candidate is shortlisted only if it passes every check:
   - at least 300 development and 100 validation trades;
   - a session-clustered t-stat above the Bonferroni bar for the number of candidates tried;
   - a session-bootstrap 95% lower bound above zero;
   - positive results in at least 60% of symbols;
   - a validation mean above the always-long, always-short and random-entry baselines with the same exit policy.

   The shortlist is written to `data/research/strategy_search/runs/<run_id>/selection.json` before any holdout trade is replayed.
2. **Holdout** scoring runs only for that frozen shortlist and its baselines.

Rolling walk-forward (60-session train, 20-session test, inside development and validation only) picks the best candidate on each window and chains the following window's result. It runs over exit policies for the rule and over every candidate, ML included, so it measures the selection procedure itself.

The ML filter is scored walk-forward too: after 60 training sessions, each block of 20 sessions is predicted by models fitted only on earlier sessions, so its development, validation and holdout results are all out of sample. (Scoring the training period with its own model had reported a 68% development win rate against 38% on validation, from a base rate near 30%.)

Each run records its git revision, dependency versions, a SHA-256 fingerprint of every symbol's data, skipped symbols and the selection file's hash. Run files are never overwritten; `latest.json` points to the newest run. A size-aware cost line (₹2 lakh positions, from `execution/costs.py`) is reported for sensitivity only. Results are research and are not wired into the live rule.

**Opening-range breakout and Nifty gap regime.** Strategy Lab (`trading-agent optimize strategy-lab SYMBOL` or `POST /api/v1/strategy-lab/compare`) adds two profiles:

- `orb`: the first candle close beyond the 09:15–09:30 range, once per side per session. A session without its 09:15 bar has no range.
- `orb_gap`: the same breakout filtered by the Nifty 50 opening gap. A gap up of more than +0.2% takes only shorts, a gap down of more than −0.2% only longs, and a flat open both.

`orb_gap` needs index candles: `trading-agent ingest NIFTY50 --interval minute --days 60` (`BANKNIFTY` also works). Without them the profile is listed under `skipped_profiles`. Both profiles use the shared bracket replay (ATR stop, reward/risk target, costs), so they are compared like for like with the existing profiles and the unconditional baselines. They remain exploratory hypotheses.

The **ML drift monitor** compares logged live model inputs with a training-period baseline, using PSI and KS per feature. A feature is `warn` above `MONITOR_PSI_WARN` and `drift` above `MONITOR_PSI_DRIFT`. When `MONITOR_DRIFT_FEATURES_TO_FALLBACK` features drift, ML is withdrawn: the research scan loads `models/ml_model.previous.joblib` if present, otherwise ML is unavailable and Rules + ML mode holds. **Restore ML** clears the fallback.

```bash
python -m trading_agent.strategy.model_monitoring baseline   # build once from stored history
python -m trading_agent.strategy.model_monitoring report     # report and apply fallback on drift
python -m trading_agent.strategy.model_monitoring clear      # clear an active fallback
```

The scheduler runs the drift check at 15:40 IST. Until a baseline exists, the monitor reports "no baseline".

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

`PAPER_MAX_SINGLE_POSITION_PCT=0.10` caps each symbol's notional. `PAPER_MAX_DAILY_LOSS_PCT=0.03` blocks new paper entries once the day's realized loss reaches 3% of IST session-start equity. `PAPER_MAX_DRAWDOWN_PCT=0.15` blocks entries at 15% realized drawdown from the ledger's peak, including previous sessions. The daily limit also applies to the worst case: today's realized P&L, minus the risk at the stop of every open position, minus the new trade's risk, must not pass it, so a day that has lost 2.5% with 1% open risk takes no new risk. These are fractions, not whole percentages. Existing positions can still be closed. The entry API checks the limits and inserts the position in one serialized SQLite transaction. Missing or invalid ledger evidence blocks entries; these limits do not measure unrealized losses or actual execution costs.

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

**Live set.** `live_symbols.txt` (49 of the most traded names; a copy in `data/` overrides it) is the live set. The ticker builds 1-minute bars for it from ticks, beside its usual interval, and never writes the first, partial minute after a (re)connect. Add `"scope": "live"` to the scan settings to observe and scan only the live set; it looks for new bars every 10 seconds instead of 30, and its scans still require qualified evidence. With `API_RESEARCH_AUTOSTART=live` (set in `docker-compose.yml`), the API arms the live scope at startup, with `min_scan_seconds` 60. That keeps the dashboard's 1-minute plans current: a plan expires about 75 seconds after its bar closes, and a Kite backfill plus a scan took about as long. Leave it unset to keep the explicit opt-in.

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

News sentiment and the LLM comparison agent use OpenRouter:

```dotenv
LLM_PROVIDER=openrouter
LLM_BASE_URL=https://openrouter.ai/api
OPENROUTER_API_KEY=sk-or-...        # or LLM_API_KEY
LLM_MODEL=google/gemini-2.0-flash-001
LLM_MIN_INTERVAL_SECONDS=4.5        # spacing between calls (avoids 429)
LLM_MAX_RETRIES=4                   # retries on 429/5xx, honouring Retry-After
```

Set `LLM_PROVIDER=mock` to make no calls; that is also the code default when `.env` does not set it. Never commit `.env` or `data/.access_token`.

Check the whole configuration before the first session:

```bash
python -m trading_agent.scripts.validate_setup             # env, Kite auth/quote, DB WAL, watchlist tokens, RSS, one LLM ping
python -m trading_agent.scripts.validate_setup --skip-llm  # same, without spending an LLM call
```

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

# Is the data up to date?
trading-agent data-check                       # watchlist
trading-agent data-check --universe nifty500   # the ranker trial's universe
trading-agent data-check --universe all        # every stored symbol (~2,300; about 5 minutes)

# Fill what it found
trading-agent backfill --universe all --interval minute --days 30 --missing-only
```

**Checking that data is current.** `data-check` (module `ingest/coverage.py`) reads the stored candles for the chosen symbols (`--interval minute`, `--days 30` by default). There is no exchange holiday calendar yet, so it builds the calendar from every date on which any checked symbol has bars. It reports:

- **Store:** the newest stored session against the last weekday whose market has closed. A mismatch means the store is stale, or that weekday was a holiday.
- **Per symbol:**
  - **up to date:** complete through the latest session;
  - **behind:** its last complete session is older;
  - **holes:** sessions missing or incomplete inside the window;
  - **empty:** no data at all.

  A session is complete with the 09:15 bar and a bar at 15:14 or later (closing-auction stocks stop at 15:14). A session fully fetched from Kite also counts, even with sparse or no bars: thin stocks often have no trade at 09:15 or near 15:14. Each fetch records its window in `data/ingest_coverage/<interval>/<SYMBOL>.json`. Today counts only after 15:30.
- **Not listed by Kite:** symbols missing from Kite's instrument list (renamed, delisted, REITs). Nothing can be fetched for them, so they are shown apart and do not fail the check.
- **Also:** the Kite token, the ticker heartbeat, and each trial arm's picks, settlements and missed sessions.

It prints the `backfill` command that fills the gaps and exits with status 1 when candles are behind, the store is stale or the token has expired, so it can run as a pre-flight step. `--no-token` skips the Kite call. In Docker: `docker compose run --rm tools trading-agent data-check --universe all`.

The default watchlist comes from `symbols.txt`; named Nifty 500 loading uses `symbols_nifty500_plus.txt`. Explicit symbols are safest for the first run and avoid a large broker request.

To rebuild the Nifty 500 watchlist and resolve instrument tokens automatically (falls back to the cached instrument list if Kite is unreachable):

```bash
python -m trading_agent.ingest.build_nifty500_symbols            # add --no-resolve to skip token lookup
```

Backfill is chunked, resumable and paced for Kite's rate limits; rerunning after an interruption continues from the stored data. The live ticker writes candles from a background thread so slow disk writes cannot stall the WebSocket (the cause of earlier 1006 disconnects).

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

Validation tools:

```bash
python -m trading_agent.research.walk_forward_optimizer --selftest   # vectorized backtest must match the slow loop reference
python -m trading_agent.research.walk_forward_optimizer --all        # walk-forward over the whole watchlist (expensive)
```

`--selftest` uses stored data when available and a synthetic frame otherwise. `strategy/session_features.py` provides `verify_causal` (no feature changes when future candles are altered) and `verify_sanity`; together with the train/serve feature checks in the tests they guard against lookahead and skew.

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
| `trading-agent backfill [SYMBOL...] [--days N] [--missing-only] [--universe watchlist\|nifty500\|all]` | Fetch a longer history; `--missing-only` requests only sessions missing or incomplete |
| `trading-agent data-check [SYMBOL...] [--universe watchlist\|nifty500\|all] [--days N] [--no-token]` | Are candles, token, ticker and trial current? Exit 1 if not |
| `trading-agent heal-gaps [SYMBOL...] --interval 5minute --limit 100` | Bounded catch-up with overlap, error summary, and failure exit status |
| `trading-agent sector-map` | Download NSE index lists into `data/cache/sector_map.csv` (industry and index membership) |
| `trading-agent screener-scan [--universe all\|indexed] [--watch MIN] [--no-quotes]` | Recompute screener technicals and sector breadth with live Kite quotes; `--watch` repeats during market hours |
| `trading-agent storage-stats` | Summarize Parquet data |
| `trading-agent compact SYMBOL` | Compact a symbol's Parquet data |
| `trading-agent diagnose [SYMBOL...]` | Compare signal behavior with baselines |
| `trading-agent backtest SYMBOL...` | Replay a period using stored data |
| `trading-agent pipeline [SYMBOL...]` | Run candidate/opportunity processing once |
| `trading-agent news [SYMBOL...]` | Fetch current financial headlines |
| `trading-agent api` | Start the API through the main CLI |
| `trading-agent optimize ...` | Walk-forward, ML, and strategy-lab commands |
| `python -m trading_agent.scripts.validate_setup [--skip-llm]` | Pre-flight configuration check |
| `python -m trading_agent.ingest.build_nifty500_symbols` | Rebuild the Nifty 500 watchlist with tokens |
| `python -m trading_agent.research.walk_forward_optimizer --selftest / --all` | Backtest self-test / whole-watchlist walk-forward |
| `python -m trading_agent.strategy.model_monitoring baseline / report / clear` | ML drift baseline, report, clear fallback |
| `python -m trading_agent.strategy.outcome_cli status / train / paper / promote` | BUY/SELL outcome-model lifecycle |
| `trading-agent optimize strategy-search [SYMBOL...] [--no-ml]` | Frozen-selection strategy search with baselines and walk-forward |
| `trading-agent optimize cross-sectional [SYMBOL...]` | Cross-sectional ranking walk-forward vs random picks |
| `trading-agent optimize ranker-trial start / pick / settle / status [--arm rf\|hgb\|all]` | Prospective paper trial of the frozen rankers (start needs one arm) |
| `trading-agent optimize screen-outcomes [SYMBOL...] [--decision HH:MM] [--cost-bps X] [--min-value-cr X]` | Historical net return of each screener screen against the liquid universe, development vs holdout |
| `trading-agent optimize decision-outcomes [--days N] [--horizon MIN] [--cost-bps X]` | Score stored rule/ML/LLM signals on realised returns after costs |
| `trading-agent optimize trade-replay SYMBOL --side BUY\|SELL --entry X --stop X --target X [--at "YYYY-MM-DD HH:MM"] [--replays N] [--seed N] [--sessions N] [--json]` | Simulated and historical odds of target first, stop first or neither by 15:15 for one bracket |
| `trading-agent optimize ranking-lab [SYMBOL...] [--snapshots FILE]` | 60 pre-registered cross-sectional variants, selection frozen on validation |

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

Router orders are stored in the `orders` table (migration 003) with side, quantity, reference and fill price, charges, status and broker order id. Kill-switch state, audit logs and drift status are files under `data/controls/` and `data/ml_monitoring/`; the drift baseline is `models/feature_baseline.json`.

Paper book balances now derive from persistent `open_positions` rows (open and closed), not a separate in-memory wallet. The wallet is gross: it excludes fees and unrealized P&L. Router fills include slippage, and **Analytics** reports P&L net of charges. Keep `PAPER_INITIAL_CAPITAL` fixed for an existing book. `PAPER_ENTRIES_ENABLED=false`, followed by an API restart, blocks new entries but allows exits. Exposure/risk limits are documented in the README. Advisory monitoring does not automatically execute stop orders.

Optional `API_AUTH_TOKEN` protects reads and writes. Enter it in the dashboard access-token form; it lasts for the current page session only. The default stack is localhost-only. Do not expose it publicly without TLS, proper identity management, and review.

## 12. Tests and developer checks

```bash
# Backend (PYTHONPATH=src avoids importing another installed copy of the package)
PYTHONPATH=src python -m pytest -q -p no:cacheprovider --ignore=tests/docker_smoke.py --ignore=tests/dashboard_auth_smoke.py --ignore=tests/dashboard_workflow_smoke.py
ruff check src migrations

# Frontend, then the offline browser smokes against the build
cd frontend && npm run typecheck && npm run build && cd ..
python tests/dashboard_workflow_smoke.py
python tests/dashboard_auth_smoke.py
```

Tests run with `PAPER_ENFORCE_SESSION=false` and an isolated `DATA_DIR` (see `tests/conftest.py`), so they pass outside market hours and never touch the real kill switch or audit log. CI uses Python 3.11, SQLite, the backend tests and the frontend production build.

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

`frontend/src/App.tsx` hosts every tab under hash routes (`#/dashboard`, `#/research`, `#/paper`, `#/risk`, `#/analytics`, `#/pipeline`, `#/history`, `#/news`). The classic pages call `/api/...` (served by `api/routers/legacy_ui.py` from the research scan snapshot); the research workspace calls `/api/v1/research`; Risk & execution and Analytics call `/api/v1/controls`, `/api/v1/execution` and `/api/v1/analytics`. An empty classic dashboard usually means no research scan has completed yet.

### Entry rejected

A 409 names the blocking rule: kill switch, before 09:15, after the cutoff, weekend, daily trade cap, participation cap, duplicate symbol, exposure, stop risk, daily loss (realized or worst case), drawdown, a stop too near the entry, or a fresh price already through the stop or the target. Check **Risk & execution → Trading controls**. A 502 means the broker rejected the order and the reservation was removed; the audit trail has the broker message.

### Sentiment 429 errors

OpenRouter throttles free and low-tier keys. Raise `LLM_MIN_INTERVAL_SECONDS` (for example to 8) and restart the API. The client already retries 429/5xx with `Retry-After` and exponential backoff, and its spacing is process-wide.

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
