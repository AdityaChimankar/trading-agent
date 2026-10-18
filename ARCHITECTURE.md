# Architecture Decision Record (ADR)

## Project: NSE Intraday Trading Signal Agent

---

### ADR-001: Package Layering (Strict Stack)

**Status:** Accepted  
**Date:** 2024-01-15

**Context:** Need clear dependency direction to avoid circular imports and ensure testability.

**Decision:**
```
scripts/              (orchestration: scheduler, pre-flight, supervisor)
    │
    ├── strategy/     (turn features into BUY/SELL/HOLD)
    ├── analysis/     (read stored data, describe it)
    └── ingest/       (talk to Kite/NSE/RSS, write candles/news)
         │
         └── risk/    (allocate capital, cap exposure, monitor positions)
              │
              └── core/     (deterministic math: indicators, patterns, sizing, signals)
                   │
                   └── storage/  (SQLite connection, schema, migrations, watchlist)
                        │
                        └── paths.py  (file locations; no imports at all)
```

**Rules:**
- Arrows point DOWN only — a layer imports layers below, never above
- `research/` sits beside stack: imports core, storage, strategy; nothing imports it
- `api/` is read-only façade: imports any layer, nothing imports it
- `frontend/` only talks to `api/` over HTTP

**Consequences:**
- Clear ownership: each layer has single responsibility
- Easy testing: mock layers below, test layer in isolation
- No circular imports possible

---

### ADR-002: Three Parallel Decision Paths

**Status:** Accepted  
**Date:** 2024-01-20

**Context:** Want to compare rule-based, LLM, and ML signals on identical inputs without contamination.

**Decision:** Three independent agents write to separate tables:
| Agent | Module | Output Table | Purpose |
|-------|--------|--------------|---------|
| Rule-based | `strategy/decision_agent.py` | `signals` | Tested path, validated by backtest |
| LLM | `strategy/llm_decision_agent.py` | `llm_signals` | Comparison only |
| ML v1 | `strategy/ml_decision_agent.py` | `ml_signals` | Comparison only |
| ML v2 | `strategy/ml_decision_agent_v2.py` | `ml_signals` | Enhanced, walk-forward validated |

**Rules:**
- No agent feeds another agent's decision
- All log against same candle timestamp (not wall clock)
- Hybrid strategy (`strategy/hybrid_strategy.py`) backtests to pick best mode per symbol

**Consequences:**
- Honest comparison: track records accumulate independently
- No "consensus" bias — each path stands on its own
- Promotion rules (STRATEGIES.md) decide when ML/LLM can gate positions

---

### ADR-003: Inverted Dependency Fix (Strategy → Research)

**Status:** Accepted (Fixed)  
**Date:** 2024-03-10

**Problem:** Strategy modules imported from `research/backtest.py` (validation depending on implementation).

**Fix:** Lifted shared primitives to `core/signals.py`:
- `SLIPPAGE`, `DEFAULT_PARAMS`, `FORWARD_WINDOW`, `LOOKBACK_MIN`, `MAX_HOLD_CANDLES`, `SWING_ORDER`
- `prepare_symbol_series()`, `precompute_pattern_bias_codes()`
- `vectorized_evaluate()`, `simulate_trades()`
- `--selftest` verification

**Result:** Both `strategy/` and `research/` import from `core/` — correct layering.

---

### ADR-004: Live Ticker Architecture (Non-blocking)

**Status:** Accepted  
**Date:** 2024-02-01

**Problem:** Kite WebSocket drops connection if `on_ticks` blocks on I/O.

**Decision:** Two-thread architecture:
- **Tick thread:** `on_ticks` does ONLY in-memory dict updates (no DB)
- **Flush thread:** Drains completed minute-buckets every 5s over persistent connection
- **Heal thread:** Separate thread for gap healing (rate-limited, never blocks flush)

**Key invariants:**
- Completed minute handed off to buffer, never overwritten in place
- Exchange timestamp used for bucketing (not local clock)
- MODE_QUOTE for >100 symbols (lighter than MODE_FULL)

---

### ADR-005: Candle Timestamp Identity

**Status:** Accepted  
**Date:** 2024-02-15

**Problem:** Historical backfill stored `2026-09-25T09:15:00+05:30`, live ticker wrote `2026-09-25T09:15:00` — same minute, two PKs.

**Decision:** Normalize ALL writes through `core.freshness.candle_timestamp()` → naive IST minute precision. Reads compare 16-char minute prefix (`substr(timestamp, 1, 16)`).

---

### ADR-006: SQLite Concurrency Model

**Status:** Accepted  
**Date:** 2024-01-25

**Decision:**
- WAL mode + 30s `busy_timeout`
- Per-symbol commits (not per-watchlist)
- Four writers during market hours: ticker flush, scheduler, CLI, API
- `execute_with_retry()` with exponential backoff for transient locks

---

### ADR-007: Signal Source Tracking

**Status:** Accepted  
**Date:** 2024-10-10

**Decision:** Every open position stores `signal_source` (`rule` | `ml` | `hybrid` | `llm`). Position monitor checks reversal in corresponding table:
- `rule` → `signals`
- `ml` → `ml_signals`
- `hybrid` → `hybrid_signals`
- `llm` → `llm_signals`

**Consequence:** ML/Hybrid/LLM signals can open/manage positions with same rigor as rule-based.

---

### ADR-008: Multi-stage Docker Build

**Status:** Accepted  
**Date:** 2026-10-18

**Problem:** Naive Dockerfile produced 75 GB image.

**Decision:** Three-stage build with `.dockerignore`:
| Stage | Base | Purpose | Copied to Final |
|-------|------|---------|-----------------|
| builder | python:3.11-slim | Install deps with gcc/g++ | `/install` → `/usr/local` |
| playwright | python:3.11-slim | Install Chromium + deps | `/opt/playwright` → `/opt/playwright` |
| runtime | python:3.11-slim | Runtime only (no build tools) | App code + artifacts |

**Result:** Build context 3.6 GB → ~1 MB; Final image ~2-3 GB.

---

### ADR-009: Exit Logic Standardization

**Status:** Accepted  
**Date:** 2024-08-15

**Problem:** Backtest used fixed 6-candle hold; live used 1.5×ATR stop + 2:1 target.

**Decision:** Remove take-profit (`USE_TAKE_PROFIT = False`). Stop-only is only change with large, consistent, independently-reproduced evidence (+0.009%/trade).

**Validation:** `diagnose_edge.py --exits` and `strategy_lab.py` agree to 0.0002%.

---

### ADR-010: Verification via Self-tests

**Status:** Accepted  
**Date:** 2024-04-01

**Decision:** No test suite — verification via self-checking entry points:

| Test | Command | Validates |
|------|---------|-----------|
| Fast path == reference | `python -m research.backtest --selftest` | Vectorized backtest |
| Grid combos match | `python -m research.walk_forward_optimizer --selftest` | WF optimizer |
| No lookahead features | `python -m strategy.session_features` | Causality |
| Admit-rates plausible | `python -m strategy.candidates` | Gate thresholds |
| Core signals match | `python -m core.signals --selftest` | Shared primitives |

---

*All ADRs are binding. New architectural decisions must be recorded here with status, context, decision, and consequences.*