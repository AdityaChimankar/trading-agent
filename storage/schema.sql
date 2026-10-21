-- Instruments you're tracking
CREATE TABLE IF NOT EXISTS watchlist (
    symbol TEXT PRIMARY KEY,
    instrument_token INTEGER NOT NULL,
    exchange TEXT NOT NULL DEFAULT 'NSE'
);

-- OHLCV candle data (both historical backfill and live-derived)
CREATE TABLE IF NOT EXISTS candles (
    symbol TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume INTEGER,
    PRIMARY KEY (symbol, timestamp)
);

-- News headlines, tagged to symbols where possible
CREATE TABLE IF NOT EXISTS news (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT,
    headline TEXT NOT NULL,
    source TEXT,
    published_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    UNIQUE(headline, published_at)
);

CREATE INDEX IF NOT EXISTS idx_candles_symbol_ts ON candles(symbol, timestamp);
CREATE INDEX IF NOT EXISTS idx_news_symbol ON news(symbol);

-- LLM sentiment score per news row, plus its short rationale
CREATE TABLE IF NOT EXISTS sentiment (
    news_id INTEGER PRIMARY KEY REFERENCES news(id),
    symbol TEXT,
    score REAL NOT NULL,          -- -1.0 (very negative) to +1.0 (very positive)
    rationale TEXT,
    scored_at TEXT NOT NULL
);

-- Final rule-based agent output: one row per decision cycle per symbol
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    action TEXT NOT NULL,          -- BUY / SELL / HOLD
    rsi REAL, adx REAL, atr REAL,
    sentiment_score REAL,
    rationale TEXT
);

-- LLM-based decision, kept SEPARATE from the rule-based `signals` table
-- on purpose - lets you compare the two before ever trusting the LLM
-- agent alone. Never merge these without deliberately deciding to.
CREATE TABLE IF NOT EXISTS llm_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    action TEXT NOT NULL,          -- BUY / SELL / HOLD
    confidence REAL,               -- 0.0-1.0, self-reported by the model
    rationale TEXT,
    rule_based_action TEXT         -- what decision_agent.py said at the same moment, for comparison
);

-- End-of-day narrative summaries
CREATE TABLE IF NOT EXISTS digests (
    date TEXT PRIMARY KEY,
    summary TEXT NOT NULL,
    generated_at TEXT NOT NULL
);

-- ML model decision, kept SEPARATE from the rule-based `signals` table -
-- same reasoning as llm_signals: compare before ever trusting alone.
CREATE TABLE IF NOT EXISTS ml_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    action TEXT NOT NULL,          -- BUY / SELL / HOLD
    confidence REAL,               -- model's predicted probability for the chosen class
    rule_based_action TEXT         -- what decision_agent.py said at the same moment, for comparison
);

-- Hybrid strategy signals (Rule + ML combined), mode-selectable.
-- Same discipline as llm_signals/ml_signals: compare before trusting.
CREATE TABLE IF NOT EXISTS hybrid_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    action TEXT NOT NULL,          -- BUY / SELL / HOLD
    confidence REAL,               -- combined confidence score
    mode TEXT NOT NULL,            -- rule_only / ml_only / rule_ml_combined
    rule_action TEXT,              -- what decision_agent.py said
    ml_action TEXT,                -- what ML model predicted
    ml_confidence REAL,            -- ML confidence
    rationale TEXT                 -- explanation of the combined decision
);

-- Candidate strategy votes (strategy/candidates.py), logged in PARALLEL with
-- the rule-based `signals` table - same discipline as llm_signals/ml_signals.
-- One row per candidate per symbol per cycle, so each strategy's live track
-- record can be compared against the shipped agent and against every other
-- before any of them is trusted with capital. Written only by
-- strategy/shadow.py; never read by any live decision path.
CREATE TABLE IF NOT EXISTS strategy_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    action TEXT NOT NULL,           -- BUY (candidate fired) / HOLD (it didn't)
    fired INTEGER NOT NULL,         -- 1/0 mirror of action, for cheap SQL aggregation
    atr REAL,
    rule_based_action TEXT          -- what decision_agent.py said on the same candle
);
CREATE INDEX IF NOT EXISTS idx_strategy_signals_lookup
    ON strategy_signals(strategy_name, symbol, timestamp);

-- Paper-tracked open positions - used by portfolio_risk.py to size NEW
-- trades against what's already "open", not just in isolation. This is
-- a paper/simulation ledger, not a real broker position feed - nothing
-- here reflects actual filled orders.
CREATE TABLE IF NOT EXISTS open_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,           -- BUY or SELL
    entry_price REAL NOT NULL,
    position_size INTEGER NOT NULL,
    position_value REAL NOT NULL,
    risk_amount REAL NOT NULL,      -- rupees at risk if stop-loss is hit
    stop_loss REAL,                 -- from the PositionPlan that opened this position
    take_profit REAL,
    opened_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open'   -- 'open' or 'closed'
);

-- Every exit the monitor has found on an open position, and when. The monitor
-- only ever RECOMMENDS - nothing here closes anything - so this table is what
-- makes a breach survive the moment it was found: without it, a stop that was
-- pierced at 11:42 left no trace the instant the dashboard was closed, and
-- "the position is still open" was the only evidence anything had happened.
-- Written by risk.position_monitor.run_monitor_cycle() on a schedule (and by
-- nothing else), one row per (position, kind, candle): a breach that keeps
-- re-firing on the same bar is recorded once rather than every pass.
CREATE TABLE IF NOT EXISTS position_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER NOT NULL REFERENCES open_positions(id),
    symbol TEXT NOT NULL,
    kind TEXT NOT NULL,              -- stop | target | reversal | stale | no_data
    recommendation TEXT NOT NULL,    -- SELL | STALE | NO_DATA
    candle_timestamp TEXT,           -- the bar the verdict came from (null = no usable candle)
    price REAL,                      -- bar close at detection (null when no mark could be vouched for)
    reason TEXT NOT NULL,
    detected_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_position_alerts_once
    ON position_alerts(position_id, kind, COALESCE(candle_timestamp, ''));
CREATE INDEX IF NOT EXISTS idx_position_alerts_recent ON position_alerts(detected_at);

-- ------- wallet ----------

-- Paper capital you seed yourself: the starting balance the wallet builds
-- from. One row, editable by you via the API. Nothing here touches real cash -
-- this is a what-if ledger on top of the paper positions you already record.
CREATE TABLE IF NOT EXISTS wallet_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),   -- singleton by convention
    capital REAL NOT NULL DEFAULT 500000.0,
    updated_at TEXT NOT NULL
);

-- Every money movement in the paper book, in order:
--   type = deposit  → +amount  (paper capital in)
--   type = withdraw → −amount  (paper capital out, paper only)
--   type = realized_pnl → +/-amount  (net of slippage, from a closed trade)
-- The live balance = wallet_settings.capital + SUM(deposits) − SUM(withdrawals)
--                    + SUM(realized P&L).
-- after you add capital, withdrawals and realized P&L are inserted by the API,
-- not by you.
CREATE TABLE IF NOT EXISTS wallet_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL CHECK (type IN ('deposit', 'withdrawal', 'realized_pnl')),
    amount REAL NOT NULL,
    note TEXT,
    symbol TEXT,                       -- which position this realized_pnl came from (null for
                                      -- deposits/withrawals)
    trade_id INTEGER REFERENCES trades(id),
    transacted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wallet_txn_date ON wallet_transactions(transacted_at);

-- The real P&L ledger: every closed trade, so "entire days" and running P&L
-- are computable across the life of the book, not just the currently-open
-- positions. Filled by the API when you close a position - the realized P&L
-- there is computed from the position's recorded entry vs the last known
-- live price at the moment you closed it, then flowed into a realized_pnl
-- transaction so the wallet balance reflects it.
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,             -- BUY or SELL (the same as the open position)
    position_id INTEGER REFERENCES open_positions(id),
    entry_price REAL NOT NULL,
    entry_at TEXT NOT NULL,
    exit_price REAL NOT NULL,         -- last known candle close at close time
    exit_at TEXT NOT NULL,
    position_size INTEGER NOT NULL,
    realized_pnl_rupees REAL NOT NULL,-- signed (profit positive), the sum the wallet sees
    exit_reason TEXT NOT NULL,        -- 'manually_closed' | 'stop_loss_hit' | 'take_profit_hit' | 'signal_reversed'
    slippage_deducted REAL NOT NULL DEFAULT 0.0,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_exit_at ON trades(exit_at);

-- ------------------- live-feed liveness -------------------

-- Heartbeat for the long-lived ingest process. The live ticker's flush thread
-- updates this singleton row every few seconds, so anything that can read the
-- DB (the API, the scheduler, you) can tell "the market is quiet" apart from
-- "the feed is dead". The candles table alone cannot express that difference:
-- a stalled feed and a symbol nobody traded both just look like "no new rows".
CREATE TABLE IF NOT EXISTS ingest_status (
    id INTEGER PRIMARY KEY CHECK (id = 1),      -- singleton: the live ticker
    connected INTEGER NOT NULL DEFAULT 0,       -- 1 while the WebSocket is up
    connected_at TEXT,                          -- when this session connected
    disconnected_at TEXT,                       -- last drop, if any
    last_tick_at TEXT,                          -- exchange time of newest tick seen
    symbols_seen INTEGER NOT NULL DEFAULT 0,    -- distinct tokens that ticked this session
    buckets_pending INTEGER NOT NULL DEFAULT 0, -- in-flight (unflushed) candles
    candles_written INTEGER NOT NULL DEFAULT 0, -- candles flushed by this session
    heal_queue_depth INTEGER NOT NULL DEFAULT 0,-- outage windows waiting to be refetched
    reconnects INTEGER NOT NULL DEFAULT 0,      -- times the socket had to come back
    updated_at TEXT NOT NULL
);

-- Every hole the pipeline detected in the candle stream, and whether it was
-- repaired. Needed because a missing row is ambiguous - it can mean the symbol
-- genuinely didn't trade, or that nothing was listening. Only the second is a
-- bug, and only this table tells the two apart after the fact.
CREATE TABLE IF NOT EXISTS candle_gaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    gap_start TEXT NOT NULL,           -- first minute believed missing
    gap_end TEXT NOT NULL,             -- last minute believed missing (inclusive)
    minutes INTEGER NOT NULL,          -- how many minutes the hole spans
    detected_at TEXT NOT NULL,
    healed_at TEXT,
    healed_candles INTEGER,            -- rows actually recovered (0 = genuinely quiet)
    status TEXT NOT NULL DEFAULT 'pending'   -- pending | healed | failed
);
CREATE INDEX IF NOT EXISTS idx_candle_gaps_lookup ON candle_gaps(status, symbol, gap_start);

-- Per-symbol, per-interval record of what has been backfilled. The candles
-- table cannot answer "is this symbol up to date for THIS interval?": it has
-- no interval column, so a year of 5-minute bars and a year of minute bars
-- look the same, and an earliest-candle test happily declares a minute
-- backfill complete because some 5-minute history is already there.
CREATE TABLE IF NOT EXISTS backfill_state (
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL,          -- minute | 3minute | 5minute | ... | day
    earliest TEXT,
    latest TEXT,
    rows_total INTEGER NOT NULL DEFAULT 0,
    completed_at TEXT,
    PRIMARY KEY (symbol, interval)
);

-- Realized execution cost tracking: captures the gap between signal price
-- and actual fill price, including spread, slippage, and market impact.
-- This is the binding constraint identified in EDGE_ANALYSIS.md - every
-- number in the project assumes flat 0.05% round trip, but real costs vary
-- by symbol, time-of-day, and volatility regime. Measuring this enables
-- cost-aware position sizing and symbol selection.
CREATE TABLE IF NOT EXISTS realized_costs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    signal_timestamp TEXT NOT NULL,    -- candle timestamp the signal was generated on
    signal_price REAL NOT NULL,        -- price from the signal (close of signal candle)
    signal_action TEXT NOT NULL,       -- BUY or SELL
    fill_timestamp TEXT NOT NULL,      -- when the position was actually opened
    fill_price REAL NOT NULL,          -- actual fill price (from position entry)
    slippage_pct REAL NOT NULL,        -- (fill_price - signal_price) / signal_price * 100 (signed)
    spread_pct REAL,                   -- estimated spread at fill time, if available
    volume_at_fill INTEGER,            -- volume at fill candle, for impact estimation
    atr_at_signal REAL,                -- ATR at signal time, for cost normalization
    session_minute INTEGER,            -- minute of day (0-1440), for time-of-day analysis
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_realized_costs_symbol ON realized_costs(symbol);
CREATE INDEX IF NOT EXISTS idx_realized_costs_signal_ts ON realized_costs(signal_timestamp);

-- A per-day snapshot of the portfolio so "entire days" can be viewed even
-- if the live stream was interrupted. Filled once per trading day (whenever
-- the window is still open) by a lightweight endpoint; or by you at close.
-- For paper trading this is mostly for your own records - nothing automated
-- needs it.
CREATE TABLE IF NOT EXISTS wallet_daily_snapshots (
    date TEXT PRIMARY KEY,
    capital REAL NOT NULL,            -- capital at start of that day
    deposits REAL NOT NULL DEFAULT 0.0,
    withdrawals REAL NOT NULL DEFAULT 0.0,
    closed_day_pnl REAL NOT NULL DEFAULT 0.0,   -- realized P&L of trades closed that day
    open_positions_mark_value REAL NOT NULL DEFAULT 0.0,  -- mark-to-market of any still-open
    open_positions_count INTEGER NOT NULL DEFAULT 0,
    snapshot_at TEXT NOT NULL
);
