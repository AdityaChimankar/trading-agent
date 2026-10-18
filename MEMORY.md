# Project Memory

## Project: NSE Intraday Trading Signal Agent

---

## Key Decisions & Rationale

### 2026-10-18: Multi-stage Docker Build
**Decision:** 3-stage Dockerfile (builder → playwright → runtime) + `.dockerignore`
**Result:** 75 GB → ~2-3 GB image, build context 3.6 GB → ~1 MB
**Why:** Naive `COPY . .` sent all data/caches/node_modules to daemon; layer accumulation from rebuilds
**Lesson:** Always use `.dockerignore` first; multi-stage separates build-time from runtime deps

### 2026-10-18: Package Structure `src/trading_agent/`
**Decision:** Moved all modules under `src/trading_agent/` with `pip install -e .`
**Result:** Clean imports (`from trading_agent.core import ...`), CLI entry points work
**Why:** Old structure had modules at repo root causing import confusion
**Lesson:** `src/` layout is standard for installable packages; avoids "import works in tests but not installed"

### 2026-10-18: Integration Layer Completion
**Decision:** Implemented all missing integration subsystems (autologin, scheduler, portfolio_risk, position_monitor, opportunity_finder)
**Result:** All 154 tests pass (3 integration test suites now passing: portfolio_exposure, position_monitor, opportunity_exclusions)
**Why:** TDD contract tests were written for subsystems that never got built
**Lesson:** Integration tests define the API contract; implementing to them forces correct architecture

### 2026-10-06: Live Ticker "Database Locked" Fix
**Decision:** Added retry logic + connection recreation in `flush_loop`
**Result:** No more `sqlite3.OperationalError: database is locked` during market hours
**Why:** Persistent connection in flush thread got into bad transaction state after lock; rollback + recreate fixes
**Lesson:** SQLite WAL + 30s timeout isn't enough; need application-level retry + connection health

### 2026-10-05: diagnose_edge.py EMA Pullback Migration
**Decision:** Updated to use `vectorized_evaluate` (EMA pullback) instead of pattern gates
**Result:** Script runs, shows small positive edge but below slippage
**Why:** Strategy evolved from pattern-based to EMA pullback; diagnostics must match
**Lesson:** Diagnostics tools must be updated when core signal logic changes

### 2024-08-15: Exit Logic Standardization
**Decision:** `USE_TAKE_PROFIT = False` (stop-only), remove 2:1 target
**Result:** +0.009%/trade improvement (largest single measured gain)
**Why:** Target fires 23.4% of trades (capping winners), stop fires 55.9% (full loss)
**Evidence:** `diagnose_edge.py --exits` and `strategy_lab.py` agree to 0.0002%
**Lesson:** Backtest exit MUST match live exit; fixed hold ≠ ATR stop

### 2024-10-10: Signal Source Tracking
**Decision:** Every position stores `signal_source` (`rule`|`ml`|`hybrid`|`llm`)
**Result:** ML/Hybrid/LLM can open/manage positions with same rigor as rule-based
**Why:** Comparison artifacts became actionable when they showed measured edge
**Lesson:** Separate tables enable clean comparison; promotion rules gate production use

---

## Technical Learnings

### SQLite at Scale
- **WAL mode + 30s busy_timeout** handles 4 concurrent writers
- **Per-symbol commits** essential — holding transaction across 500 symbols + LLM calls = lock
- **Connection recreation** after repeated locks prevents "stuck" state
- **Execute with retry** (exponential backoff) for transient contention

### Kite Connect Integration
- **Access token expires ~7:30 AM IST daily** — auto-login via Playwright + TOTP required
- **Historical data rate limit: ~3 req/s** — throttle to 2.5 req/s, retry on 429
- **WebSocket MODE_QUOTE for >100 symbols** — lighter than MODE_FULL, same data we need
- **Gap healing** critical — socket can be open but silent (dropped subscriptions)

### ML Pipeline Evolution
| Version | Validation | Features | Labels | Thresholds | Trades |
|---------|------------|----------|--------|------------|--------|
| v1 | Chronological 80/20 | 9 | Fixed | Global 0.5 | Overlapping |
| v2 | Walk-forward (21 folds) | 19 | Cost-aware | Per-symbol 0.40-0.80 | Non-overlapping |

**Key Insight:** Walk-forward + regime features + confidence thresholds = measured edge (+495% vs -135%)

### Backtesting Integrity
- **SLIPPAGE = 0.05% single source** — imported by ML labels, backtest, strategy_lab
- **Gross vs Net** — diagnostics report gross (slippage cancels in comparison); backtest reports net
- **Non-overlapping trades** — skip bars after exit, otherwise same move counted dozens of times
- **Time-split honesty** — early 80% vs late 20% EV; curve-fit shows early gain, late loss

### Self-Tests > Unit Tests
- No test suite — verification via self-checking entry points on REAL data
- `python -m research.backtest --selftest` — vectorized == reference loop
- `python -m strategy.session_features` — prefix invariance (no lookahead)
- `python -m strategy.candidates` — admit-rate plausibility (0.05%-60%)
- Two independent implementations agreeing = trusted (e.g., exit variants)

---

## Data & Metrics

### Current Scale
- **Symbols:** 2,270 (full NSE equity)
- **Candles/day:** ~337,000 (1-min, 9:15-15:30)
- **Parquet storage:** 1.6 GB (partitioned by symbol)
- **Backfill:** 120 days = ~40M candles
- **ML v2 training:** 21 folds × 500 symbols = ~10K model fits

### Performance Baselines
| Metric | Rule-based | ML v2 (WF) |
|--------|------------|------------|
| Gross EV/trade | -0.0042% | +0.0166% (best candidate) |
| Win rate | 27-40% | 50-60% |
| Trades (full history) | 5,066 | 1,858 |
| Total return | -135% | +495% |
| Edge vs random | -0.0042% | +0.0192% (BUY) |

### Cost Reality Check
- **Slippage assumption:** 0.05% round trip
- **Real cost includes:** spread + impact + fill gap (signal → execution)
- **Measured expected move:** 11.4× round trip (median), so cost not binding
- **Binding constraint:** Directional accuracy (~50% wrong), not cost

---

## Recurring Patterns

### When Things Break
| Symptom | Root Cause | Fix Pattern |
|---------|------------|-------------|
| "Database locked" | Long transaction + concurrent writer | Per-symbol commits, retry, recreate conn |
| "Invalid token" on historical | Kite subscription issue | Check plan includes historical data |
| Stale symbols at open | Overnight gap heal pending | Wait 30 min or run manual heal |
| ML accuracy ~48% | Chronological split leakage | Use walk-forward (v2) |
| Backtest ≠ live | Different exit logic | Standardize on stop-only |

### Debugging Checklist
1. `python -m core.freshness` — Is feed live? (heartbeat age, stale symbols)
2. `python -m scripts.validate_setup` — Pre-flight all connections
3. `docker compose logs -f <service>` — Container-level issues
4. `ingest_status` table — Ticker heartbeat, buffer depth, heals queued
5. `candle_gaps` table — What's missing, what healed

---

## Context for Future Sessions

### To Resume ML v2 Training
```bash
# Full watchlist (takes hours)
trading-agent optimize ml-model-v2 --all

# Or top liquid symbols first
trading-agent optimize ml-model-v2 RELIANCE TCS INFY HDFCBANK ICICIBANK
```

### To Check OOS Validation
```bash
# Query ml_signals table for live track record
sqlite3 data/trading_agent.db "SELECT symbol, action, confidence, timestamp FROM ml_signals ORDER BY timestamp DESC LIMIT 20;"
```

### To Run Full Validation Suite
```bash
python -m research.backtest --selftest
python -m research.walk_forward_optimizer --selftest 360ONE
python -m strategy.session_features
python -m strategy.candidates
python -m core.signals --selftest 360ONE
```

### Environment Variables Needed
```env
KITE_API_KEY=xxx
KITE_API_SECRET=xxx
KITE_TOTP_SECRET=xxx  # base32 from 2FA setup
KITE_USER_ID=xxx
KITE_PASSWORD=xxx
OPENROUTER_API_KEY=xxx
LOKY_MAX_CPU_COUNT=8  # silences joblib warning on Windows
```

---

## Open Questions

1. **Realized cost measurement** — How to capture fill price vs signal price in paper trading?
2. **Exit walk-forward** — Can we extend optimizer to optimize stop multiple + max bars?
3. **Ensemble voting** — When ML/LLM promoted, how to weight rule + ML + LLM?
4. **Regime detection** — Current ATR percentile is simple; HMM or Markov switching better?
5. **Multi-timeframe** — 5-min signals + 15-min trend confirmation backfired; why?

---

## Integration Layer Implementation Notes (2026-10-18)

### Autologin (`kite_auto_login.py`)
- Multiple fallback selectors for userid, password, TOTP, submit buttons
- Stealth mode with `disable-blink-features=AutomationControlled`
- Page content logging for debugging selector failures

### Scheduler (`scripts/scheduler.py`)
- All decision cycle functions defined BEFORE scheduler creation (fixes import order)
- Sync wrapper functions for async decision cycles
- Market hours guard on every job
- Feed watchdog (`heal_if_feed_down`) every 10 min
- ML retraining Sunday 2 AM outside market hours

### Portfolio Risk (`risk/portfolio_risk.py`)
- `calculate_portfolio_adjusted_position()` with 50% exposure cap, 6% risk budget
- Reserved value/risk from prior candidates (list sizing)
- `_get_risk_config()`, `compute_correlation_matrix()`, `get_open_positions()` for tests
- `calculate_position()` base per-trade sizing
- `exposure_summary()` for wallet API

### Position Monitor (`risk/position_monitor.py`)
- `get_position_status()` — intrabar stop/target using candle high/low
- `get_all_position_statuses()` — batch check all open positions
- `run_monitor_cycle()` — records alerts, deduplicates by candle timestamp
- Stale feed handling: withholds PnL/price, returns STALE recommendation
- Signal reversal detection (BUY→SELL or SELL→BUY)
- Sync wrappers (`*_sync`) for test compatibility

### Opportunity Finder (`analysis/opportunity_finder.py`)
- Module-level `find_opportunities()` with test-expected API
- `StrategyMode` enum + `_score_conviction`, `_get_best_mode`, `_get_hybrid_signal` for monkeypatching
- Held symbol exclusion via `get_open_position_symbols()`
- Deduplicated excluded symbols list

### Database & Wallet (`storage/db.py`, `api/routers/wallet.py`)
- `position_alerts`, `trades` tables in `init_db()`
- `get_connection()`, `wallet()`, `exposure_summary()` for test compatibility
- `get_open_position_symbols()` in opportunity_finder (monkeypatch target)

### Test Fixes
- `test_portfolio_exposure.py`: 13/13 pass — mocks `get_open_positions`, `calculate_position`, etc.
- `test_position_monitor.py`: 16/16 pass — uses `open_positions` table, sync wrappers, correct timestamps
- `test_opportunity_exclusions.py`: 3/3 pass — monkeypatches module-level functions, not storage.db

---

*Memory updated: 2026-10-18*
*Next review: After ML v2 full training completes*