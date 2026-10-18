# Task Tracker

## Project: NSE Intraday Trading Signal Agent

---

### Current Sprint (Active)

| ID | Task | Status | Owner | Notes |
|----|------|--------|-------|-------|
| T-001 | ML v2 full watchlist training (`--all`) | 🟡 In Progress | - | Run `trading-agent optimize ml-model-v2 --all`; monitor `ml_signals` |
| T-002 | Shadow candidate OOS validation (3+ weeks) | 🟡 In Progress | - | `strategy_signals` accumulating live; review weekly |
| T-003 | Realized execution cost measurement | 🔴 Not Started | - | New module: measure spread/impact/fill-gap per signal |
| T-004 | Walk-forward optimizer for exits | 🔴 Not Started | - | Extend `walk_forward_optimizer.py` beyond entry thresholds |
| T-005 | Model drift detection integration | 🔴 Not Started | - | Use `core/ml_versioning.py` in scheduler |
| **T-006** | **Automated ML retraining (weekly)** | **🟡 Ready** | **-** | **Add `ml_retrain_cycle` to scheduler cron (Sunday 2 AM)** |
| **T-007** | **Per-symbol liquidity filtering** | **🟡 Ready** | **-** | **Filter low-volume symbols using `liquidity_cache.json`** |

---

### Completed (This Cycle)

| ID | Task | Completed | Notes |
|----|------|-----------|-------|
| C-001 | Multi-stage Docker build (75GB → 3GB) | 2026-10-18 | `.dockerignore` + 3-stage Dockerfile |
| C-002 | Package wiring (`src/trading_agent/`) | 2026-10-18 | `pip install -e .` works, all CLI commands |
| C-003 | README.md rewrite | 2026-10-18 | Current architecture, Docker, CLI reference |
| C-004 | Fix live_ticker "database is locked" | 2026-10-06 | Retry logic + connection recreation in flush_loop |
| C-005 | Fix diagnose_edge.py for EMA pullback | 2026-10-05 | Updated to use `vectorized_evaluate` signature |
| C-006 | Remove unused postgresql import | 2026-10-18 | `migrations/versions/001_initial.py` |
| C-007 | Signal source tracking (rule/ml/hybrid/llm) | 2026-10-10 | Position monitor uses same source for reversals |
| C-008 | Exit logic standardization (stop-only) | 2024-08-15 | `USE_TAKE_PROFIT = False` default, +0.009%/trade |
| **C-009** | **Integration layer completion (3 test suites)** | **2026-10-18** | **All 154 tests pass; autologin, scheduler, portfolio_risk, position_monitor, opportunity_finder** |

---

### Backlog (Prioritized)

#### P0 - Critical Path
| ID | Task | Effort | Dependencies |
|----|------|--------|--------------|
| B-001 | ML v2 full watchlist training | 4-8 hrs | GPU/CPU time, Kite historical quota |
| B-002 | Promotion decision (STRATEGIES.md criteria) | 1 wk | B-001 complete, 3+ weeks OOS data |
| B-003 | Walk-forward optimizer for exits | 2-3 days | Research design, ADR needed |

#### P1 - High Value
| ID | Task | Effort | Dependencies |
|----|------|--------|--------------|
| B-004 | Realized cost measurement per signal | 3-5 days | New DB tables, fill simulation |
| B-005 | Cost-aware position sizing | 2 days | B-004 data |
| B-006 | Per-symbol liquidity filtering | 1 day | `liquidity_cache.json` exists |
| B-007 | Automated ML retraining (weekly) | 1 day | `scripts/ml_retrain.py` exists |
| **B-016** | **ML drift detection integration** | **2 days** | **core/ml_versioning.py + scheduler** |
| **B-017** | **Cost-aware feature engineering** | **2 days** | **B-004 data + ml_features.py** |

#### P2 - Nice to Have
| ID | Task | Effort | Dependencies |
|----|------|--------|--------------|
| B-008 | SEBI compliance layer | 1-2 wks | Legal review |
| B-009 | Multi-broker abstraction | 1 wk | Interface design |
| B-010 | Mobile P&L alerts | 2 days | Notification service |
| B-011 | Options data ingestion | 2 wks | Kite Connect options API |

#### P3 - Technical Debt
| ID | Task | Effort | Notes |
|----|------|--------|-------|
| B-012 | Migrate from SQLite to PostgreSQL | 2 days | `docker-compose.yml` has PG |
| B-013 | **Integration test suite** | **DONE** | **3 suites: portfolio_exposure, position_monitor, opportunity_exclusions** |
| B-014 | Frontend type-check in CI | 1 day | `npx tsc --noEmit` |
| B-015 | Document all config options | 1 day | `core/config.py` |

---

### Known Issues (Tracking)

| ID | Issue | Severity | Workaround |
|----|-------|----------|------------|
| I-001 | Kite historical_data fails for some symbols | High | "Invalid token" — subscription issue, not code |
| I-002 | Parquet migration manual (not in daily pipeline) | Medium | Run `python -m storage.parquet_store migrate` after backfill |
| I-003 | Polars indicators untested in production path | Low | Opt-in via `quant_indicators._pl` variants |
| I-004 | Supervisor experimental (no auto-restart) | Low | Scheduler repair job fills gaps |
| I-005 | 103 stale symbols during catch-up | Low | Normal during heal; resolves in ~30 min |
| I-006 | LOKY_MAX_CPU_COUNT warning on Windows | Low | Set `LOKY_MAX_CPU_COUNT=8` in .env |

---

### Release Checklist (v2.1.0 Target)

- [ ] ML v2 trained on full watchlist (`--all`)
- [ ] 3+ weeks OOS validation on `ml_signals`
- [ ] Promotion criteria met (STRATEGIES.md)
- [ ] Realized cost module implemented
- [ ] Walk-forward optimizer extended to exits
- [x] **All self-tests pass (154/154)**
- [ ] Docker image builds < 5 GB
- [ ] README/docs updated
- [ ] CHANGELOG.md updated

---

### Velocity Tracking

| Week | Completed | In Progress | Blocked |
|------|-----------|-------------|---------|
| 2026-W43 | T-006, T-007 (ML retrain + liquidity) | T-001 to T-005 | I-001 |
| 2026-W42 | C-009 (Integration layer) | T-001 to T-005 | I-001 |
| 2026-W41 | C-001 to C-008 | T-001 to T-005 | I-001 |
| 2026-W40 | ML v2 implementation | Docker optimization | - |
| 2026-W39 | Shadow testing framework | Live ticker fixes | - |

---

### Notes
- **Paper trading only** — no real order placement
- All timestamps naive IST minute precision
- Working directory MUST be repo root for `python -m` commands
- Run `python -m scripts.validate_setup` before market hours

*Last updated: 2026-10-18*