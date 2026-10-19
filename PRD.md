# Product requirements

Last reviewed: 2026-10-19

## Product statement

Trading Agent is a local-first NSE intraday research and paper-trading workbench. It collects market/news data, evaluates multiple decision approaches independently, measures them against honest baselines, applies portfolio constraints, and presents results through a CLI, API, and dashboard.

It is not an order execution system and must never imply that simulated fills or balances reflect a broker account.

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

A user can view opportunities, record/monitor paper positions, and inspect paper wallet metrics without any real-order path.

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
| P2 | PostgreSQL runtime | not supported yet |
| Out of scope | Real broker order placement | intentionally absent |

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
| Paid/unintended LLM calls | mock provider is default; explicit credentials required |
| Strategy overfitting | walk-forward evaluation and documented promotion rules |
| Users confuse paper and real positions | repeated paper-only labeling and no order API |
| Documentation drift | commands validated during release review; docs link to one canonical quick start |

## Next product milestones

1. Add API/startup smoke tests around a fresh migrated database.
2. Bring lint/type-check baselines to zero incrementally and then make them strict CI gates.
3. Add measured operational evidence for weekly retraining and drift alerts.
4. Unify or retire remaining legacy SQLite schema paths.
5. Optimize the frontend Plotly chunk.
6. Decide whether PostgreSQL support is valuable enough to migrate all direct SQLite consumers.
