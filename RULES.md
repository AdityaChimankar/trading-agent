# Project Rules & Conventions

## Project: NSE Intraday Trading Signal Agent

---

### Code Style & Quality

#### Python
- **Version:** 3.11+ (type hints required)
- **Formatter:** `black` (line-length=100)
- **Linter:** `ruff` (target-version=py311)
- **Type Checker:** `mypy` (strict-optional, basic mode)
- **Import Sort:** `isort` (known-first-party=["trading_agent"])

```bash
# Run all checks
ruff check && black --check && mypy src/trading_agent
```

#### Type Hints
- All public functions must have type hints
- Use `from __future__ import annotations` for forward refs
- Prefer `list[str]` over `List[str]` (Python 3.9+)
- Use `Optional[X]` not `X | None` for consistency

#### Naming Conventions
| Type | Convention | Example |
|------|------------|---------|
| Modules | snake_case | `quant_indicators.py` |
| Classes | PascalCase | `PositionPlan` |
| Functions | snake_case | `compute_atr()` |
| Constants | UPPER_SNAKE | `SLIPPAGE = 0.0005` |
| Private | _leading_underscore | `_write_candles()` |
| Type vars | PascalCase + T | `T_co = TypeVar("T_co", covariant=True)` |

---

### Git & Commits

#### Branch Strategy
- `main` — protected, only via PR
- Feature branches: `feature/<short-desc>`
- Fix branches: `fix/<issue-number>-<short-desc>`
- No direct commits to main

#### Commit Messages
```
<type>(<scope>): <imperative summary>

<body if needed>

<footer if needed>
```

Types: `feat`, `fix`, `refactor`, `perf`, `docs`, `test`, `chore`, `revert`

Examples:
```
feat(ml): add walk-forward validation to train_ml_model_v2
fix(ticker): handle stale connection in flush_loop
perf(docker): multi-stage build reduces image 75GB -> 3GB
```

#### Pre-commit Hooks
```yaml
# .pre-commit-config.yaml (enforced)
- ruff (lint + format)
- black (format)
- mypy (type check)
- pytest (unit tests)
```

---

### Architecture Rules (Binding)

1. **Layering** — Import DOWN only (see ARCHITECTURE.md ADR-001)
2. **Three Decision Paths** — Parallel, separate tables (ADR-002)
3. **Core Signals Single Source** — `core/signals.py` only (ADR-003)
4. **Non-blocking Ticker** — No I/O in `on_ticks` (ADR-004)
5. **Timestamp Identity** — Normalize via `candle_timestamp()` (ADR-005)
6. **Per-symbol Commits** — Never hold transaction across watchlist (ADR-006)
7. **Signal Source Tracking** — Every position stores source (ADR-007)
7. **No Relative Paths** — Use `paths.py` for all file locations

---

### Data & Storage Rules

#### Database (SQLite)
- WAL mode mandatory
- `busy_timeout=30000`
- `synchronous=NORMAL`
- All timestamps naive IST minute precision
- Compare minute prefixes: `substr(timestamp, 1, 16)`

#### Parquet Storage
- Partitioned by `symbol`/`interval`
- Migrate with `python -m storage.parquet_store migrate`
- Read via `core.quant_indicators.load_candles()` (auto-detects Parquet)

#### Watchlist
- Single source of truth: `watchlist` table
- Seed via `ingest/build_nifty500_symbols.py`
- No module hardcodes symbol lists

---

### Trading Logic Rules

#### Signal Generation
- All three agents log against **candle timestamp**, not wall clock
- Rule-based agent is the ONLY validated path
- LLM/ML agents are comparison artifacts until promoted
- Promotion rules in STRATEGIES.md (t ≥ 2, 500+ trades, OOS validation)

#### Position Sizing
- ATR-based, risk-per-trade configurable
- `USE_TAKE_PROFIT = False` (default, measured worse)
- Volatility-aware scaling: scales DOWN only, caps at 1.0
- Max position % of capital enforced

#### Risk Management
- Portfolio risk budget (default 6%)
- Same-symbol concentration cap
- Correlation-cluster cap (20%, measured from returns)
- Position monitor checks: stop → target → signal reversal → HOLD

#### Backtesting
- Vectorized (NumPy/Polars), not loop-based
- SLIPPAGE = 0.05% single source of truth
- Non-overlapping trades (skip bars after exit)
- Walk-forward: rolling train/test, no lookahead

---

### Ingestion Rules

#### Kite Connect
- Access token expires ~7:30 AM IST daily
- Auto-login via `kite_auto_login.py` (Playwright + TOTP)
- Manual fallback: `kite_auth.py`
- Rate limit: ~2.5 req/s historical, WebSocket for live

#### Gap Healing
- Healer compares session timeline vs stored minutes
- Fetches ONLY holes (cheap when healthy)
- Runs: on reconnect, every 10min while down, post-close sweep
- Records ALL gaps in `candle_gaps` (including 0-recovery)

---

### Testing & Validation Rules

#### Self-tests (Mandatory Before PR)
```bash
python -m research.backtest --selftest
python -m research.walk_forward_optimizer --selftest 360ONE
python -m strategy.session_features
python -m strategy.candidates
python -m core.signals --selftest 360ONE
```

#### Research Tools
- `diagnose_edge.py` — Measures signal vs random baseline
- `strategy_lab.py` — Candidate grid search with time-split
- `walk_forward_optimizer.py` — Rolling threshold optimization
- All report GROSS of slippage (slippage cancels in comparison)

#### No Unit Test Suite
- Verification via self-tests on REAL data
- Synthetic fixtures don't catch real-world edge cases
- Two independent implementations agreeing = trusted

---

### Docker Rules

- Multi-stage build (builder → playwright → runtime)
- `.dockerignore` excludes: data/, models/, node_modules/, .git/, caches/
- No `COPY . .` — explicit `COPY src/ ./src/`
- Build context target: ~1 MB
- Final image target: ~2-3 GB
- Run with `--no-cache` after Dockerfile changes

---

### Security Rules

- `.env` for secrets (never committed)
- `.access_token` gitignored, written by `kite_auth.py`
- Kite credentials: API Key, Secret, TOTP, User ID, Password
- OpenRouter API key for LLM/sentiment
- No credentials in code, logs, or Docker images
- Paper trading only — no order placement code

---

### Documentation Rules

- Update relevant `.md` with every architectural change
- ADRs in ARCHITECTURE.md for binding decisions
- PRD.md for product direction
- STRATEGIES.md for candidate promotion rules
- EDGE_ANALYSIS.md for measurement methodology
- DESIGN.md for bug-forced decisions
- README.md for user-facing docs

---

### CI/CD Rules

- All checks pass locally before push
- Docker build must succeed
- Tests run in container: `docker compose exec api pytest`
- No deployment without manual approval

---

*Violations of binding rules (Architecture, Data, Trading Logic) require ADR.*