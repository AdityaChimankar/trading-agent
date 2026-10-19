# Task: Implement Missing Subsystems for Integration Tests

> Archived implementation brief. Completed and regression-tested as of 2026-10-19. For current work, use [TASKS.md](TASKS.md); for current interfaces, use [DESIGN.md](DESIGN.md).

## Overview
Three integration test files require full implementation of subsystems that were designed but never built:
1. `test_portfolio_exposure.py` - Portfolio risk coordination with exposure caps
2. `test_position_monitor.py` - SQLite-backed position monitoring with API serialization
3. `test_opportunity_exclusions.py` - Opportunity filtering against held positions

---

## Task 1: Portfolio Risk Coordination Layer

### File: `src/trading_agent/risk/portfolio_risk.py`

### Required Functions/Classes:

#### 1. `calculate_portfolio_adjusted_position(symbol, action, capital, reserved_value=0, reserved_risk_amount=0) -> PositionPlan`
Returns a `PositionPlan` with:
- `approved_size` (int)
- `approved_value` (float) 
- `blocked` (bool)
- `block_reason` (str)
- `total_exposure_pct` (float)
- `total_risk_used_pct` (float)

**Logic:**
- Gets open positions via `get_open_positions()`
- Calculates base position via `calculate_position(symbol, action, capital)`
- Applies exposure cap (50% max_total_exposure_pct from config)
- Applies risk budget (6% total_risk_budget_pct from config)
- Reserves space for `reserved_value` and `reserved_risk_amount` from prior candidates

#### 2. `get_open_positions() -> list[dict]`
Returns list of open positions with: symbol, action, entry_price, position_size, position_value, risk_amount

#### 3. `calculate_position(symbol, action, capital) -> PositionSizeResult`
Base position sizing (already exists, needs to return PositionSizeResult)

#### 4. `exposure_summary(capital) -> dict`
Returns: `{"committed_notional": float, "free_notional": float, "exposure_pct": float}`

#### 5. `_get_risk_config() -> SimpleNamespace`
Returns config with: `total_risk_budget_pct`, `max_cluster_exposure_pct`, `max_total_exposure_pct`

#### 6. `compute_correlation_matrix(symbols, lookback=None) -> pd.DataFrame`

---

## Task 2: SQLite-Backed Position Monitor

### Files: 
- `src/trading_agent/storage/db.py` - Schema & helpers
- `src/trading_agent/risk/position_monitor.py` - Core logic
- `src/trading_agent/api/serializers.py` - `jsonable()` function
- `src/trading_agent/api/routers/wallet.py` - `wallet()` endpoint

### Database Schema (from test):
```sql
-- candles table
CREATE TABLE candles (
    symbol TEXT,
    timestamp TEXT,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume INTEGER
);

-- open_positions table
CREATE TABLE open_positions (
    id INTEGER PRIMARY KEY,
    symbol TEXT,
    action TEXT,
    entry_price REAL,
    position_size INTEGER,
    position_value REAL,
    risk_amount REAL,
    stop_loss REAL,
    take_profit REAL,
    opened_at TEXT,
    status TEXT,
    signal_source TEXT
);

-- signals table
CREATE TABLE signals (
    symbol TEXT,
    timestamp TEXT,
    action TEXT,
    rationale TEXT
);

-- position_alerts table
CREATE TABLE position_alerts (
    position_id INTEGER,
    kind TEXT,
    recommendation TEXT,
    candle_timestamp TEXT
);

-- trades table
CREATE TABLE trades (...);
```

### Required Functions:

#### 1. `get_all_position_statuses(fetch_current_signals=True, now=datetime) -> list[PositionStatus]`
Returns list of PositionStatus objects with:
- `position` (dict)
- `recommendation` (str: "SELL"/"HOLD"/"STALE"/"NO_DATA")
- `alert_kind` (str: "stop"/"target"/"reversal"/"stale"/"no_data")
- `reason` (str)
- `current_price` (float or None)
- `pnl` (float or None)
- `pnl_pct` (float or None)
- `stale` (bool)
- `candle_age_minutes` (float)

#### 2. `get_position_status(position_row, now=datetime) -> PositionStatus`
Single position status check.

#### 3. `run_monitor_cycle(now=datetime) -> dict`
Returns: `{"checked": int, "alerts": int, "recorded": int}`

**Logic:**
- For each open position, check latest candle
- If candle age > 60 min during session -> STALE
- If LOW <= stop_loss -> SELL (stop), record alert
- If HIGH >= take_profit -> SELL (target), record alert
- If signal reversal -> SELL (reversal), record alert
- Don't re-record same alert for same candle
- Record to `position_alerts` table

#### 4. `jsonable(obj) -> dict`
Serialize PositionStatus/dataclass to JSON-serializable dict.

#### 5. `wallet() -> dict`
API endpoint returning:
```json
{
  "open_positions": [...],
  "today": {"open_pnl": 0.0, "open_positions_count": 0},
  "fixed": {"book_balance": 500000.0}
}
```

---

## Task 3: Opportunity Finder Pipeline

### File: `src/trading_agent/analysis/opportunity_finder.py`

### Required Functions/Classes:

#### 1. `find_opportunities(capital, n=50) -> dict`
Returns: `{"bullish": [...], "bearish": [...], "excluded": [...]}`

**Logic:**
- Calls `rank_watchlist_with_sizing(capital, n, reserve_prior=True)`
- Filters out held symbols via `storage.db.get_open_position_symbols()`
- Scores conviction via `_score_conviction(symbol, action, score)`
- Gets best mode via `_get_best_mode(symbol)`
- Gets hybrid signal via `_get_hybrid_signal(symbol, mode)`
- Returns excluded symbols in `excluded` list

#### 2. `rank_watchlist_with_sizing(capital, n, reserve_prior=False) -> dict`
(Already partially implemented, needs to call `pr.calculate_portfolio_adjusted_position`)

#### 3. `_score_conviction(symbol, action, score) -> (score, [], [])`

#### 4. `_get_best_mode(symbol, candle_limit=5000, use_cache=True, use_fixed_mode=True) -> StrategyMode`

#### 5. `_get_hybrid_signal(symbol, mode) -> (action, confidence, rationale, ...)`

#### 6. `StrategyMode` enum with `RULE_ML_COMBINED`

#### 7. `_opportunity_cache` - simple dict cache

---

## Task 4: Rankings Module

### File: `src/trading_agent/analysis/rankings.py`

### Required Functions:

#### 1. `annotate_contradictions(scored, positions) -> list`
For each scored symbol, check if position exists and add contradiction info.
Must handle `pnl=None` gracefully (don't raise TypeError).

#### 2. `rank_watchlist(max_symbols=500) -> list[dict]`
Returns list of dicts with: symbol, bullish_score, bearish_score, dominant_bias, contradiction

---

## Implementation Order

1. **First**: Portfolio Risk (Task 1) - Foundation for others
2. **Second**: Database Schema & Position Monitor (Task 2) - Core infrastructure
3. **Third**: Opportunity Finder (Task 3) - Depends on Task 1
4. **Fourth**: Rankings (Task 4) - Depends on Task 2

---

## Test Commands

```bash
# Run each test file individually
pytest tests/test_portfolio_exposure.py -v
pytest tests/test_position_monitor.py -v  
pytest tests/test_opportunity_exclusions.py -v

# Run all
pytest tests/ -v
```
