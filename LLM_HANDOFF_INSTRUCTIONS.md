# LLM Handoff: trading-agent

Read this first, then [MEMORY.md](MEMORY.md). MEMORY.md is the single source of truth for
status, open work and the decision log; if the two disagree, MEMORY.md wins. Update it in the
same commit as any change to features, priorities or evidence. Contributor rules are in
[RULES.md](RULES.md).

Last updated: 2026-10-27.

## What the project is

An intraday NSE workbench: Kite data pipeline (Parquet candle store, live ticker), rule / ML /
LLM signal paths, a persistent **paper** book with intraday risk controls, a guarded order
router, a React + MUI dashboard and a set of research modules. Python 3.11 in Docker (local
venv may be newer), FastAPI, SQLite (`data/trading.db`), APScheduler. Paper mode only: no
trading edge has been shown, and live orders are unverified.

## The one fact that matters most

**No single-stock intraday signal in this repo has an edge after costs.** Do not try to raise
the win rate by retuning thresholds; it has been done, and it fails out of sample:

| Already tried | Result (details in MEMORY.md decision log) |
|---|---|
| Rule thresholds (RSI/ADX/pattern) by walk-forward | Negative after costs (RELIANCE); exit-policy walk-forward −13.5 bps/trade over 7,449 trades |
| Strategy search, 2,508 entry x bar x exit candidates, frozen selection | 0 shortlisted, 0 nominally positive |
| ML classifiers (incl. the cost-aware "v2" design) | 68% wins in-sample, 38% on validation; v2 gains never revalidated and retired |
| Moving-average filters, ORB, gap regimes (Strategy Lab) | Not adopted |
| 10 screener screens, 254 sessions, point in time | All `no_edge`; the liquid universe itself nets −16.5 bps long |
| Live rule signals (decision outcomes, 1 session so far) | 38.7% win, −15.5 bps at 60 min; too few sessions to judge |

Why: typical intraday moves are about the size of the 14 bps round-trip cost. A higher win rate
is easy to buy (closer targets, wider stops) but only at negative expectancy. With a 2R target
and costs of 0.2R, break-even is a 40% win rate. **Judge everything by net R or net bps per
session, on sessions not used to choose it, against a random baseline.**

Every historical window, including all holdouts, has been inspected. **Only sessions recorded
from now on are clean evidence.** Re-running a historical study does not produce new evidence.

## Live experiments (do not change their specs)

Each has a hashed, frozen spec and a verdict fixed in advance. Changing the spec, the universe or
the verdict rule invalidates it.

| Experiment | Started | Verdict | Where |
|---|---|---|---|
| Ranker paper trial, `rf` arm (cross-sectional lag model, top 10 long/short, 15:15 exit) | first session 2026-10-26 | first 60 settled sessions: mean net > 0, t ≥ 2, above random | `research/ranker_trial.py`, `data/research/ranker_trial/` |
| Ranker paper trial, `hgb` arm | first session 2026-10-26 | same, own 60 sessions | `data/research/ranker_trial_hgb/` |
| Entry-filter forward test: 09:30–14:30 window, and the trade-replay gate | first session 2026-10-28 | first 20 recorded sessions, paired t ≥ 2 | `research/forward_filters.py`, `data/research/forward_filters/` |

The ranker is the only lead with a positive held-out result (ranking lab, `hgb_lags` k10:
+13.3 bps/day, t 2.51, 66% winning days), and it did not clear the multiple-testing bar. That is
why it is being tested prospectively instead of adopted.

These need the scheduler running every session with a valid Kite token (`trading-agent login`).
Missed sessions are recorded as missed, never filled in.

## Scheduler (IST, weekdays)

| Time | Job |
|---|---|
| 09:16:30 | `ranker_trial_pick` |
| 09:00–15:59 | news, rule / ML / LLM decisions, shadow, feed watchdog (every 5–15 min), position monitor (every minute) |
| `PAPER_SQUARE_OFF_TIME` | intraday square-off |
| 15:32, 15:35, 15:40 | end-of-session data repair, daily digest, ML drift check |
| 15:50 | `ranker_trial_settle` |
| 15:55 | `decision_outcomes` |
| 15:57 | `forward_filters` (about 40 replays at ~5.5 s each) |
| 16:05 | `compact_store` (must stay after every job that reads the session's files) |

Exact definitions: `src/trading_agent/scripts/scheduler.py`.

## Risk settings worth knowing

- `PAPER_STOP_FLOOR_PCT=0.007`: planned stops are at least 0.7% of the entry. 1-minute ATR stops
  (median 0.16%) made costs about 0.9R; at 0.7% they are 0.2R. `0` restores pure 1.5 x ATR stops.
- `PAPER_MIN_STOP_DISTANCE_PCT`, `PAPER_MIN_REWARD_RISK`, daily-loss and drawdown limits: see
  `.env.example` and `core/config.py` (`PaperRiskSettings`).
- Trade replay (`research/trade_replay.py`) is display only today; gating entries on
  `conclusive_negative` waits for the forward test's verdict.

## Commands

The CLI uses hyphens. `trading-agent --help` and `trading-agent optimize --help` list everything.

```bash
# Research (research only, no orders)
trading-agent optimize decision-outcomes --days 30 --horizon 60 --cost-bps 14
trading-agent optimize forward-filters [--record]
trading-agent optimize ranker-trial ...
trading-agent optimize trade-replay SYMBOL ...
trading-agent optimize screen-outcomes
trading-agent optimize strategy-search / ranking-lab / cross-sectional   # historical; see above

# Data and operations
trading-agent login
trading-agent data-check
trading-agent backfill --days N --missing-only

# Tests and lint
python -m pytest -q
python -m ruff check src tests

# Stack (code is baked into the image: rebuild after changes)
docker compose --profile live build
docker compose --profile live up -d
```

Cross-sectional ranking reads 5-minute bars by default, and only a few sessions of those are
stored; the minute store has ~266 sessions for most liquid names. Use `--interval minute` if you
must rerun it, and remember the result is not new evidence.

## Gotchas

- **Never open `data/trading.db` from a Windows process while the stack runs.** A host pytest
  run locked it on 2026-10-26; tests use a throwaway DB (conftest).
- **Do not restart the stack during market hours** (09:15–15:30 IST) or between 15:50 and 16:05,
  when the trial settlement, outcome scoring and forward test run.
- The Windows bind mount (`./data`) is 7–10x slower than native disk.
- Timestamps are stored in UTC; session logic is Asia/Kolkata. Legacy candle files hold naive IST
  strings and are read as IST.
- Since the NSE closing auction (2026-08-03), many names have no continuous bars after 15:14:
  exits use the 15:15 price or the session's last bar.
- `test_dashboard_keeps_the_last_completed_scan_while_a_new_one_runs` can time out under a full
  suite run; it passes alone.
- Session gates are weekday/time rules, not an exchange holiday calendar.
- `LLM_MODEL` must name a model OpenRouter still serves; the LLM path returned only HOLD on
  2026-10-26.

## What to work on

In priority order (full list and done-when criteria: MEMORY.md, Open work):

1. Keep the three live experiments running cleanly every session (token, scheduler, host sleep).
   Read their verdicts only when their fixed sample is complete.
2. Operational reliability: the ticker reloads the Kite token without a restart; the scheduler
   survives host sleep; live 1-minute bars for a live set.
3. Verify charge rates in `execution/costs.py` against Zerodha's current sheet.
4. New signal ideas only as **pre-registered prospective tests** in the same style: a hashed spec,
   a fixed start session, a fixed sample and verdict rule, and a random baseline.

Do not: retune rule thresholds or hybrid weights on historical data, pick a best profile per
symbol, apply Kelly sizing to a strategy with negative expectancy, report in-sample win rates, or
enable live orders.
