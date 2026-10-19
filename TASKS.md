# Task tracker

Last reviewed: 2026-10-19

## Recently completed

| Item | Result |
|---|---|
| Startup contract audit | CLI and API imports verified; async config/database failures fixed |
| Schema consolidation | paper-position ORM aligned with SQLite bootstrap and Alembic |
| Historical replay safety | candidate generation accepts explicit frames; backtest no longer reads future store state |
| Backtest exits | stop loss and take profit are retained on simulated trades |
| Scheduler repair | correct decision-cycle functions, sync wrappers, and Asia/Kolkata timezone |
| ML maintenance | incomplete retraining placeholder replaced with current registry/storage APIs |
| Docker repair | removed unused PostgreSQL/Redis, added profiles and working frontend proxy |
| Documentation | local/Docker setup, architecture, design, and operational docs refreshed |
| Verification | 156 backend tests passed; frontend production build passed |

## Current priorities

| Priority | Task | Done when |
|---|---|---|
| P0 | Add API fresh-database smoke tests | health, readiness, position create/list/close pass in isolation |
| P0 | Add backtest regression fixtures | a deterministic fixture proves no future-candle reads and stop/target handling |
| P0 | Reconcile legacy schema consumers | every queried table is created through one documented schema path |
| P1 | Establish a passing lint baseline | selected Ruff rules pass, then expand rule groups incrementally |
| P1 | Establish useful typing baseline | package-local imports resolve and high-risk modules type-check |
| P1 | Validate live profile | ticker/scheduler run a full market session with recorded heartbeat/errors |
| P1 | Validate ML retraining | drift report and scheduled training tested on representative stored data |
| P2 | Reduce Plotly bundle size | chart dependency is split without feature regression |
| P2 | Decide database roadmap | either complete PostgreSQL migration or remove dormant generic claims |

## Research backlog

- Full-watchlist ML v2 training using explicit symbol batches.
- Three or more weeks of out-of-sample shadow evidence.
- Realized execution-cost measurement by symbol and session regime.
- Cost-aware position sizing based on measured rather than assumed slippage.
- Walk-forward optimization of exit behavior.
- Strategy promotion only when [STRATEGIES.md](STRATEGIES.md) criteria are met.

## Known limitations

| Limitation | Impact / workaround |
|---|---|
| Kite token expires frequently | refresh with `trading-agent login` or `python manual_login.py` |
| SQLite has limited writer concurrency | run one ticker and one scheduler; avoid duplicate stacks |
| PostgreSQL is not supported end to end | use the documented SQLite URL |
| Auto-login depends on broker page markup | use the manual browser flow when it changes |
| Default symbol file can be large | start with explicit symbols to control quota/runtime |
| LLM integration is optional | default mock prevents unexpected calls/costs |
| Plotly build chunk is large | build succeeds; performance work remains |
| Historical research numbers are staleable | rerun experiments before making promotion decisions |

## Release checklist

- [x] Backend tests pass.
- [x] Frontend type-check/build passes.
- [x] CLI help starts.
- [x] Docker Compose configuration resolves.
- [x] README and user guide match implemented commands.
- [x] Secrets remain ignored and undocumented.
- [ ] Docker image build verified on a machine with daemon/network access.
- [ ] Fresh-container API health verified.
- [ ] Live Kite session verified with current credentials.
- [ ] Lint/type baselines converted into passing required checks.

## Command reminders

```bash
pytest
cd frontend && npm run build
docker compose up --build -d
docker compose --profile live up -d ticker scheduler
python -m trading_agent.scripts.validate_setup
```
