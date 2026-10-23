# Product requirements

Last reviewed: 2026-10-24

## Product statement

Trading Agent is a single local-first NSE intraday trading application. It collects market and news data and evaluates rule, ML and LLM decision paths independently, measuring them against honest baselines. It sizes trades within portfolio and intraday risk limits, routes them through one guarded order router, and reports performance net of costs. Results are available through a CLI, an API and one dashboard.

Paper mode is the default. Simulated fills and balances must never be presented as broker results. Live routing exists behind an explicit double opt-in and is not yet certified.

Institutional readiness is a roadmap, not a current product claim. Release gates are in [READINESS.md](READINESS.md); status and open work are in [MEMORY.md](MEMORY.md).

## Users

- quantitative researchers testing intraday hypotheses;
- individual developers comparing rule, ML, and LLM approaches;
- operators running a local paper-trading dashboard during NSE sessions.

## Required user outcomes

### First run

A user can install the package, copy one environment template, start the API, and open the dashboard without installing a database server or supplying broker credentials.

### Data collection

A configured user can refresh a Kite token, fetch selected historical symbols, start a live ticker, and see where persisted files are stored.

### Research

A user can run an edge diagnostic, backtest, walk-forward optimization, or model training command on stored data. Historical replay must not read future candles.

### Paper operation

A user can view opportunities, open trades through the router in paper mode, monitor and square off positions, and review net-of-cost analytics. A user can halt all entries with one control.

### Deployment

A user can start the API/dashboard with Docker Compose and explicitly opt into broker-connected ticker/scheduler services.

## Functional requirements

| Priority | Requirement | Current state |
|---|---|---|
| P0 | Installable Python package and working CLI | implemented |
| P0 | SQLite + Parquet local persistence | implemented |
| P0 | Historical/live Kite ingestion | implemented; credentials required |
| P0 | Rule signal and backtest | implemented |
| P0 | Paper risk/position monitoring | implemented |
| P0 | API health/readiness and dashboard | implemented |
| P1 | Walk-forward research and ML v2 training | implemented |
| P1 | Independent LLM comparison path | implemented; mock by default |
| P1 | Scheduled decisions and monitoring | implemented; `live` profile |
| P1 | Drift-triggered/weekly retraining | implemented; needs operational evidence |
| P0 | Intraday risk controls (kill switch, session gates, trade cap, square-off, audit) | implemented |
| P0 | Order router with pre-trade checks, Zerodha costs and reconciliation | implemented; live mode unverified |
| P1 | Performance analytics net of costs | implemented |
| P1 | Volume-based recommendations and participation cap | implemented; heuristic, not backtested |
| P1 | ML drift monitoring with automatic fallback | implemented; needs a real baseline |
| P1 | Classic dashboard alongside research workspace (Material UI) | implemented |
| P2 | PostgreSQL runtime | not supported yet |
| P2 | Partial fills, cancels, idempotent order intents | not implemented |

## Quality requirements

- No secret values in Git, logs, docs, or images.
- Time-aware research inputs with no look-ahead leakage.
- Timezone-aware timestamps and Asia/Kolkata scheduler behavior.
- A clean API/dashboard start without broker credentials.
- Functional backend tests and frontend build in CI.
- Persistent Docker data through host mounts.
- Errors should identify the missing credential, data, model, or service rather than failing during unrelated imports.

## Success measures

Product health:

- clean installation instructions work on Windows and Unix-like systems;
- `pytest` returns zero;
- frontend production build returns zero;
- API health and readiness return successful responses on a fresh database;
- Docker configuration resolves and API becomes healthy.

Research health:

- every promoted strategy beats a declared baseline after costs on out-of-sample data;
- backtest/live feature and exit definitions are documented and kept aligned;
- signal paths remain attributable;
- model versions retain training metadata.

Do not treat historical experimental returns in research notes as product guarantees.

## Risks

| Risk | Mitigation |
|---|---|
| Invalid/expired Kite token | daily login flow, manual fallback, clear token location |
| Broker UI changes break automation | manual login remains supported |
| SQLite write contention | single intended ticker/scheduler, short transactions, clear unsupported PostgreSQL status |
| Look-ahead bias | explicit `generate_from_data` contract and regression tests |
| Paid/unintended LLM calls | mock provider is the code default; OpenRouter needs an explicit key; calls are rate limited |
| Unintended live orders | paper default; live needs `EXECUTION_MODE=live` plus the confirmation phrase; kill switch |
| Strategy overfitting | walk-forward evaluation and documented promotion rules |
| Users confuse paper and real positions | execution mode shown on Risk & execution; orders record `mode` |
| Documentation drift | commands validated during release review; docs link to one canonical quick start |

## Next product milestones

See the prioritized **Open work** table in [MEMORY.md](MEMORY.md#open-work).
