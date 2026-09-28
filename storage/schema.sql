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
