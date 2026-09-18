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
