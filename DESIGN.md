# Design Document

## Project: NSE Intraday Trading Signal Agent

---

## System Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         TRADING AGENT ARCHITECTURE                          │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  EXTERNAL                          INTERNAL                                 │
│  ────────                          ────────                                 │
│                                                                             │
│  ┌──────────┐    ┌─────────────┐    ┌──────────────────────────────────┐   │
│  │ Kite     │───►│ ingest/     │───►│ storage/ (SQLite + Parquet)      │   │
│  │ Connect  │    │ • historical│    │ • candles (1-min, partitioned)   │   │
│  │ WebSocket│    │ • live      │    │ • watchlist (single source)      │   │
│  │ REST     │    │ • news      │    │ • signals/ml/llm/hybrid tables   │   │
│  └──────────┘    │ • gap heal  │    │ • positions/wallet               │   │
│                  └──────┬──────┘    └──────────────┬───────────────────┘   │
│                         │                          │                         │
│                         ▼                          ▼                         │
│                  ┌──────────────────────────────────────────┐               │
│                  │ core/ (deterministic, no I/O)            │               │
│                  │ • quant_indicators (RSI, ATR, ADX, MACD) │               │
│                  │ • pattern_detection (candles + swings)   │               │
│                  │ • signals (prepare, evaluate, simulate)  │               │
│                  │ • position_sizing (ATR-based, risk-aware)│               │
│                  │ • config, logging, circuit_breaker       │               │
│                  └──────────────┬───────────────────────────┘               │
│                                 │                                           │
│         ┌───────────────────────┼───────────────────────┐                  │
│         ▼                       ▼                       ▼                  │
│  ┌─────────────┐         ┌─────────────┐         ┌─────────────┐         │
│  │ strategy/   │         │ analysis/   │         │ risk/       │         │
│  │ • decision  │         │ • rankings  │         │ • portfolio │         │
│  │ • ml v1/v2  │         │ • sentiment │         │ • position  │         │
│  │ • llm       │         │ • digest    │         │ • wallet    │         │
│  │ • candidates│         │ • technical │         │             │         │
│  │ • shadow    │         │ • opportunities               │         │
│  └──────┬──────┘         └──────┬──────┘         └──────┬──────┘         │
│         │                       │                       │                  │
│         └───────────────────────┼───────────────────────┘                  │
│                                 ▼                                           │
│                    ┌────────────────────────┐                              │
│                    │ scripts/ (orchestration)│                              │
│                    │ • scheduler (cron jobs) │                              │
│                    │ • validate_setup        │                              │
│                    │ • supervisor (experimental)                           │
│                    └────────────┬─────────────┘                              │
│                                 │                                           │
│                                 ▼                                           │
│                    ┌────────────────────────┐                              │
│                    │ api/ (FastAPI façade)   │                              │
│                    │ • REST endpoints        │                              │
│                    │ • WebSocket (optional)  │                              │
│                    │ • Serializes to JSON    │                              │
│                    │ • NO trading logic      │                              │
│                    └────────────┬─────────────┘                              │
│                                 │                                           │
│                                 ▼                                           │
│                    ┌────────────────────────┐                              │
│                    │ frontend/ (React+Vite)  │                              │
│                    │ • Dashboard             │                              │
│                    │ • History               │                              │
│                    │ • Pipeline              │                              │
│                    │ • Hash routing          │                              │
│                    └────────────────────────┘                              │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Module Design

### Core Layer (`src/trading_agent/core/`)

| Module | Responsibility | Key Exports |
|--------|----------------|-------------|
| `config.py` | Pydantic Settings, all env vars | `get_settings()`, `Settings` |
| `quant_indicators.py` | Vectorized RSI, ATR, ADX, MACD, DI | `compute_rsi()`, `compute_atr()`, `load_candles()` |
| `pattern_detection.py` | Candlestick + swing patterns | `detect_patterns()`, `compute_swing_extrema()` |
| `signals.py` | **Single source of truth** for signal logic | `prepare_symbol_series()`, `vectorized_evaluate()`, `simulate_trades()`, `SLIPPAGE`, `DEFAULT_PARAMS` |
| `position_sizing.py` | ATR-based sizing, volatility scaling | `PositionPlan`, `calculate_position_size()` |
| `circuit_breaker.py` | Resilience for external APIs | `CircuitBreaker`, `@with_circuit_breaker` |
| `freshness.py` | Data staleness, market hours | `in_market_hours()`, `split_stale()`, `candle_timestamp()` |
| `ml_versioning.py` | Model registry, drift detection | `ModelRegistry`, `register_model()` |
| `logging_config.py` | Structured logging setup | `setup_logging()`, `get_logger()` |
| `shutdown.py` | Graceful shutdown handling | `GracefulShutdown`, `register_shutdown_handler()` |

---

### Ingestion Layer (`src/trading_agent/ingest/`)

| Module | Responsibility |
|--------|----------------|
| `kite_auth.py` | Manual Kite login flow |
| `kite_auto_login.py` | Automated TOTP login (Playwright) |
| `instrument_lookup.py` | Symbol → token mapping |
| `fetch_historical.py` | Resumable backfill, rate-limited |
| `live_ticker.py` | WebSocket → 1-min candles, heartbeat, stall watchdog |
| `gap_healer.py` | Detect missing minutes, refetch from Kite |
| `fetch_news.py` | 9 RSS feeds, keyword-tag to symbols |
| `watchlist_resolved.py` | Watchlist token map (generated) |

**Key Design: Live Ticker Threading**
```
┌─────────────────────────────────────────────────────────────┐
│                    LIVE_TICKER PROCESS                       │
├─────────────────────────────────────────────────────────────┤
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────────┐  │
│  │ TICK THREAD │    │ FLUSH THREAD│    │ HEAL THREAD     │  │
│  │             │    │             │    │                 │  │
│  │ on_ticks()  │───►│ _flush_once()│   │ heal_loop()     │  │
│  │ • dict ops  │    │ • write     │    │ • rate-limited  │  │
│  │ • NO I/O    │    │ • heartbeat │    │ • gap_healer    │  │
│  │ • < 1ms     │    │ • 5s interval│   │ • own connection│  │
│  └─────────────┘    └──────┬──────┘    └─────────────────┘  │
│                            │                                 │
│                     ┌──────┴──────┐                          │
│                     │ _completed  │  (thread-safe buffer)    │
│                     │   buffer    │                          │
│                     └─────────────┘                          │
└─────────────────────────────────────────────────────────────┘
```

---

### Strategy Layer (`src/trading_agent/strategy/`)

| Module | Responsibility |
|--------|----------------|
| `decision_agent.py` | Rule-based BUY/SELL/HOLD (validated path) |
| `ml_decision_agent.py` | ML v1 inference (LightGBM, chronological) |
| `ml_decision_agent_v2.py` | **ML v2** — walk-forward, regime features, thresholds |
| `llm_decision_agent.py` | LLM decisions via OpenRouter |
| `ml_features.py` | Feature engineering (v1: 9 features) |
| `train_ml_model.py` | ML v1 training (chronological split) |
| `train_ml_model_v2.py` | **ML v2** — walk-forward, cost-aware labels |
| `session_features.py` | Session VWAP, ATR pctile, HTF momentum, gap, cost-cover |
| `candidates.py` | 11 candidate strategies (pure functions) |
| `hybrid_strategy.py` | Backtest-selects best mode per symbol |
| `fast_hybrid_strategy.py` | Cached, <50ms latency for production |
| `shadow.py` | Logs candidate votes live to `strategy_signals` |

**ML v2 Enhancements:**
| Feature | v1 | v2 |
|---------|-----|-----|
| Validation | Chronological 80/20 | Walk-forward (21 folds) |
| Features | 9 | 19 (incl. session/regime) |
| Labels | Fixed threshold | Cost-aware dynamic |
| Thresholds | Global 0.5 | Per-symbol (0.40-0.80) |
| Trades | Overlapping | Non-overlapping |
| Exit | Fixed hold | 1.5×ATR stop, max 24 bars |

---

### Analysis Layer (`src/trading_agent/analysis/`)

| Module | Responsibility |
|--------|----------------|
| `opportunity_finder.py` | Combines rule/ML/LLM/sentiment → ranked picks |
| `rankings.py` | Momentum, mean reversion, volume rankings |
| `technical_summary.py` | Human-readable indicator summary |
| `sentiment.py` | OpenRouter scoring on tagged news |
| `digest.py` | End-of-day narrative summary |

---

### Risk Layer (`src/trading_agent/risk/`)

| Module | Responsibility |
|--------|----------------|
| `portfolio_risk.py` | Risk budget, same-symbol cap, correlation clusters |
| `position_monitor.py` | Live P&L, stop/target/reversal checks per minute |
| `wallet.py` | Paper wallet: equity, transactions, daily snapshots |

**Position Monitor Flow:**
```
Every minute:
  FOR each open position:
    1. Check STOP loss hit
    2. Check TARGET hit (if USE_TAKE_PROFIT)
    3. Check SIGNAL REVERSAL in source table
       - rule → signals table
       - ml → ml_signals table
       - hybrid → hybrid_signals table
       - llm → llm_signals table
    4. Else HOLD
```

---

### Research Layer (`src/trading_agent/research/`)

| Module | Purpose |
|--------|---------|
| `diagnose_edge.py` | **Signal vs random baseline** — the honest benchmark |
| `backtest.py` | Vectorized simulation, same logic as live |
| `walk_forward_optimizer.py` | Rolling threshold optimization |
| `strategy_lab.py` | Candidate grid search + time-split honesty |
| `donchian_breakout.py` | Alternative strategy research |

**Diagnose Edge Methodology:**
1. Benchmark = random entry (unconditional forward return)
2. Compare signal EV vs baseline (gross of slippage)
3. Ablate each component (which gate carries signal?)
4. Horizon sweep (which hold period works?)
5. Exit variants (fixed vs ATR stop vs stop+target)

---

### API Layer (`src/trading_agent/api/`)

| Endpoint | Delegates To |
|----------|--------------|
| `GET /api/health` | — |
| `GET /api/watchlist` | storage, rankings, position_monitor |
| `GET /api/rankings` | analysis.rankings |
| `GET /api/positions` | risk.position_monitor |
| `POST /api/positions` | portfolio_risk.calculate_position |
| `POST /api/positions/{id}/close` | portfolio_risk.close_position |
| `GET /api/opportunities` | analysis.opportunity_finder |
| `GET /api/digest` | analysis.digest |
| `GET /api/symbols/{symbol}/*` | storage, quant_indicators, pattern_detection, portfolio_risk |
| `GET /api/liveness` | core.freshness, ingest.gap_healer |
| `GET /api/wallet` | risk.wallet, position_monitor |
| `GET /api/pipeline` | rankings, signals/ml/llm/hybrid tables |

**Design:** Pure JSON façade — no trading logic, each endpoint calls same function as CLI.

---

### Data Schema

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `watchlist` | Single source of truth | symbol, instrument_token, exchange |
| `candles` | OHLCV (1-min, partitioned) | symbol, timestamp, o/h/l/c/v |
| `backfill_state` | Tracks (symbol, interval) completion | symbol, interval, from_date, to_date |
| `ingest_status` | Ticker heartbeat (singleton) | connected, last_tick, buffers, heals |
| `candle_gaps` | Every detected hole | symbol, gap_start, gap_end, status |
| `signals` | Rule-based decisions | symbol, timestamp, action, rationale |
| `ml_signals` | ML v1/v2 decisions | symbol, timestamp, action, confidence |
| `llm_signals` | LLM decisions | symbol, timestamp, action, rationale |
| `hybrid_signals` | Hybrid mode + components | symbol, timestamp, mode, rule/ml action |
| `strategy_signals` | Shadow candidate votes | symbol, timestamp, strategy, vote |
| `open_positions` | Paper ledger | symbol, side, entry, sl, tp, signal_source |
| `trades` | Closed trades | entry/exit, pnl, exit_reason |
| `wallet_*` | Paper accounting | transactions, snapshots, settings |

---

### Frontend (`frontend/`)

- React 18 + TypeScript + Vite
- Hash routing (`#/dashboard`, `#/history`, `#/pipeline`)
- Polling: positions/charts 15s, rankings/opportunities 60s
- Plotly charts, real-time liveness indicator
- `npm run dev` → localhost:5173 (proxies /api to :8000)

---

## Non-Functional Requirements

| Requirement | Target | Implementation |
|-------------|--------|----------------|
| Data freshness | < 10 min stale | `core.freshness`, ticker heartbeat |
| Live latency | < 50ms/symbol | `fast_hybrid_strategy`, cached indicators |
| Backtest speed | < 1min for 500 symbols | Vectorized NumPy/Polars, 4 workers |
| Docker image | < 5 GB | Multi-stage, .dockerignore |
| Type safety | 100% public API | mypy basic, strict-optional |
| Concurrency | 4 DB writers | WAL, per-symbol commits, 30s timeout |
| Reproducibility | Deterministic | `--selftest` on real data, fixed seeds |

---

## Deployment Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                      DOCKER COMPOSE STACK                     │
├──────────────────────────────────────────────────────────────┤
│                                                               │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐         │
│  │  api    │  │ ingest  │  │scheduler│  │ frontend│         │
│  │ :8000   │  │ (oneshot)│  │ (cron)  │  │ :5173   │         │
│  └────┬────┘  └────┬────┘  └────┬────┘  └────┬────┘         │
│       │            │            │            │               │
│       └────────────┼────────────┼────────────┘               │
│                    ▼            ▼                            │
│             ┌─────────────┐  ┌─────────┐                    │
│             │   postgres  │  │  redis  │                    │
│             │   :5433     │  │  :6379  │                    │
│             └─────────────┘  └─────────┘                    │
│                                                               │
│  Volumes: ./data → /app/data, ./models → /app/models        │
│  Network: all services on `trading-network`                 │
└──────────────────────────────────────────────────────────────┘
```

---

*This design document reflects the implemented architecture as of 2026-10-18.*