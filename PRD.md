# Product Requirements Document (PRD)

## Project: NSE Intraday Trading Signal Agent

### Vision
A local-first research pipeline for NSE intraday signals that ingests live and historical candles, computes deterministic technical/pattern features, and runs **three independent decision paths side by side** — rule-based, LLM, and ML — so their calls can be compared on real outcomes instead of argued about.

### Core Problem
- Most trading systems merge signals into a single "consensus" — hiding which approach actually works
- Backtests often test different logic than what runs live (different exits, different data)
- No honest baseline: strategies compared against each other, not against random entry
- Exit logic untested: backtest uses fixed hold; live uses ATR stop + target

### Target Users
- Quantitative researchers building intraday strategies for Indian equities (NSE)
- Individual traders wanting paper-trading validation before capital allocation
- Developers wanting a clean, auditable pipeline architecture

### Success Metrics
| Metric | Target | Status |
|--------|--------|--------|
| Rule-based signal edge vs random | > 0.05% gross EV | ❌ (-0.0042%) |
| Enhanced ML (v2) walk-forward return | > 0% on 7/10 symbols | ✅ (+495% vs -135%) |
| Backtest-live parity (exit logic) | Same exit in both | ✅ (stop-only) |
| Data coverage (live) | > 99% candles captured | ✅ (with gap healing) |
| Docker image size | < 5 GB | ✅ (~2-3 GB multi-stage) |

### Key Features (Priority Order)

#### P0 - Must Have (Core Pipeline)
- [x] Kite Connect ingestion (historical + live WebSocket)
- [x] Gap healing (detect + refetch missing candles)
- [x] Rule-based signal (RSI/ADX/pattern gates)
- [x] Vectorized backtest (same logic as live)
- [x] Walk-forward optimizer (entry thresholds)
- [x] Paper wallet + position monitor
- [x] FastAPI + React dashboard

#### P1 - Should Have (Signal Comparison)
- [x] LLM decision agent (OpenRouter, parallel logging)
- [x] ML decision agent v1 (LightGBM, chronological split)
- [x] ML decision agent v2 (walk-forward, regime features, confidence thresholds)
- [x] Shadow testing (candidate strategies logged live)
- [x] Hybrid strategy (backtest-selects best mode per symbol)

#### P2 - Could Have (Advanced)
- [ ] Realized execution cost measurement (spread, impact, fill gap)
- [ ] Walk-forward optimizer for exits (not just entries)
- [ ] Model drift detection (ml_versioning.py)
- [ ] SEBI algo-trading compliance layer
- [ ] Multi-broker abstraction

#### P3 - Won't Have (Out of Scope)
- [ ] Automated order placement (paper only)
- [ ] Options/futures support (equity only)
- [ ] Crypto/forex markets
- [ ] Real-time P&L streaming to mobile

### Constraints
- **Paper trading only** — no real orders placed
- **NSE equity intraday** — 5-minute candles, 9:15-15:30 IST
- **Kite Connect required** — paid plan (₹500/mo) for historical + live
- **Local-first** — no cloud dependencies, runs on laptop/server
- **Type-safe** — Python 3.11+, mypy, ruff, black

### Risks & Mitigations
| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Kite API changes break ingestion | Medium | High | Circuit breaker, fallback to manual login |
| ML v2 edge doesn't hold out-of-sample | Medium | High | Shadow logging, promotion rules (STRATEGIES.md) |
| SQLite contention at scale | High | Medium | WAL mode, per-symbol commits, 30s timeout |
| Docker image too large for CI/CD | Low | Medium | Multi-stage build, .dockerignore |

### Timeline
| Phase | Duration | Deliverable |
|-------|----------|-------------|
| Foundation | Done | Core pipeline, rule-based signal, backtest |
| ML v1 | Done | LightGBM chronological training |
| ML v2 | Done | Walk-forward, regime features, thresholds |
| Docker/Prod | Done | Multi-stage image, .dockerignore |
| Shadow OOS | Active | 3+ weeks live candidate tracking |
| Cost Model | Planned | Realized cost per signal |

---

*Last updated: 2026-10-18*