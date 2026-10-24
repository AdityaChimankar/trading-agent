# Verification and readiness

Reviewed: 2026-10-24. This file holds the verification procedure, the latest evidence, deployment constraints and the gates before live or unattended use. Project status and history live in [MEMORY.md](MEMORY.md).

## Operating boundary

The supported deployment is a single host running local research, a persistent paper book and **paper-mode** order routing. Live Zerodha orders exist in code but are off by default. They need both `EXECUTION_MODE=live` and `EXECUTION_LIVE_CONFIRMATION=I-ACCEPT-REAL-MONEY-RISK`, and have only been tested against a stubbed Kite client.

Path of a decision:

```text
Kite historical/live -> Parquet -> research scan (single source of truth)
  -> /api/v1/research + /api (classic dashboard) -> React UI
  -> order router -> risk checks (kill switch, session, trade cap, ledger limits, participation)
  -> paper fill (slippage) | live Kite MARKET/MIS -> orders + open_positions -> audit log
```

## Latest evidence (2026-10-24, branch `unified-platform`)

- 487 Python tests passed (`PYTHONPATH=src`, the two Docker/browser smokes excluded from the pytest run).
- `tests/dashboard_workflow_smoke.py` and `tests/dashboard_auth_smoke.py` passed against a fresh production build.
- Playwright pass over all eight routes on a copy of the real 2026-10-21 scan and stored candles: zero console errors, zero failed API calls, zero horizontal overflow at 390 px.
- Seeded router trades checked slippage fills (e.g. RELIANCE 1226.61 against a 1226 reference at 5 bps), charges and analytics output.

Not covered: Docker image rebuild for this branch, a live market session, a live order, broker-side reconciliation, the drift monitor on a real baseline.

Earlier evidence, summarised in [MEMORY.md](MEMORY.md#decision-log), applies only to the revisions tested then: 419 tests and an isolated Docker/browser stack (2026-10-21); deployment with a Kite WebSocket 403 (2026-10-22); a failed first outcome-model candidate and negative RELIANCE walk-forward (2026-10-20).

## Verification

From the repository root:

```powershell
$env:PYTHONPATH = "src"   # bash: export PYTHONPATH=src
python -m pytest -q -p no:cacheprovider --ignore=tests/docker_smoke.py --ignore=tests/dashboard_auth_smoke.py --ignore=tests/dashboard_workflow_smoke.py
python -m ruff check src tests migrations
python -m compileall -q src migrations
python -m trading_agent.research.walk_forward_optimizer --selftest
cd frontend; npm.cmd run build; cd ..
python tests/dashboard_workflow_smoke.py
python tests/dashboard_auth_smoke.py
```

`PYTHONPATH=src` matters when another copy of the package is installed in the environment; without it the tests can import that copy instead of this tree. CI keeps its 45% coverage gate.

Isolated Docker check after building both images:

```bash
docker compose -f docker-compose.smoke.yml -p trading-agent-audit up --abort-on-container-exit --exit-code-from smoke
docker compose -f docker-compose.smoke.yml -p trading-agent-audit down
```

The smoke stack uses no `.env`, host ports or real-data mounts and has no external network access. Remove it after each run.

## Deployment constraints

- One API worker, one ticker, one scheduler and at most one historical download. Job state, the LLM rate limiter and Kite pacing are process-local.
- SQLite in WAL mode on a local disk; network shares are not validated.
- Back up `data/` and `models/` with writers stopped; use the SQLite backup API, not a file copy, while WAL is active.
- The kill switch lives in `data/controls/kill_switch.json`; an unreadable file blocks entries. The audit trail is daily JSONL in `data/controls/`, which is not tamper-proof.
- Session gates and the 15:15 square-off need the scheduler running and use weekday/time rules, not the NSE holiday calendar.
- Position monitoring is advisory; stops are not exchange orders. Square-off is the only automatic exit.
- Live bars at connect/reconnect can be partial; the post-close historical replay corrects them.
- After code changes, rebuild images and recreate services. Do not delete data or prune images to deploy.

## Gates before institutional or unattended live use

These are requirements, not claims.

| # | Gate | Exists now | Still missing |
|---|---|---|---|
| 1 | Execution and reconciliation | Router with pre-trade checks, paper/live brokers, `orders` table, reconciliation endpoint, kill switch, square-off | Idempotent client intent IDs, partial fills, cancels, reconnect replay, fault-injection tests, a supervised live round trip |
| 2 | Financial controls | Realized daily-loss/drawdown gates, trade cap, participation cap, Zerodha cost model | Unrealized P&L in limits, decimal ledger, broker statement reconciliation, short/margin rules, sector/correlation limits |
| 3 | Data quality | Immutable Parquet, overlap gap healing, freshness checks | Exchange calendar, interior-gap audit, corporate actions, point-in-time universe |
| 4 | Security and governance | Optional bearer token, loopback ports, secrets ignored | TLS gateway, users/roles, secret rotation, immutable audit, SBOM review |
| 5 | Reliability | Heartbeats, scheduler signal handling, background candle writer | Durable job queue, metrics/alerts, soak and forced-disconnect tests, restore drills |
| 6 | Model and research governance | Drift monitor with fallback, outcome-model promotion gates, lookahead and skew checks | Real drift baseline, a qualified model, prospective out-of-sample evidence |
| 7 | Reproducibility | Pinned scikit-learn, CI build | Locked dependencies, pinned image digests, broader browser coverage |

Until these gates have evidence and an owner, keep the deployment private and in paper mode.
