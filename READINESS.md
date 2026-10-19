# Integration audit and readiness

Reviewed: 2026-10-19. Scope: local-first research and paper operations, not approval for live broker execution. Existing user data, positions, credentials, and unrelated research edits were preserved. No broker orders are part of this work.

## Operating boundary

The supported path is Kite historical/live data -> Parquet -> analysis and advisory monitoring -> SQLite paper-position ledger -> `/api/v1` -> `OperatingConsole.tsx`. Legacy frontend screens remain in the repository for reference but are not mounted: they target incompatible legacy API contracts. The console intentionally exposes only implemented operations.

| Area | Defect found | Change / current contract |
|---|---|---|
| Candle persistence | Repeated writes could overwrite earlier partition files | Immutable batches, atomic publication, per-symbol/interval file lock, timestamp-keyed last-write-wins reads |
| Replay | Mixed Arrow schemas and timestamp strings failed to parse or filter | Normalize each file to UTC before deduplication/filtering; validate OHLCV and storage keys |
| Compaction | Destructive replacement could lose originals on failure | Publish complete replacement before removing captured inputs; regression test injects write failure |
| Live feed | Wrong timestamp field, cumulative volume summed as tick volume, boundary tick assigned to previous bar | Decode SDK exchange timestamp, cumulative-volume deltas, emit previous bar before appending boundary tick |
| Reconnect | Async scheduling from a broker thread could fail | SDK-managed reconnect; rebaseline daily volume; persistent shared heartbeat |
| History | UTC datetime formatting lost the exchange timezone; incomplete intraday candles persisted | Exchange-local request strings; retain completed intraday bars only |
| Repair | Failures could appear as successful zero-row outcomes | CLI failure exit status, error/skip summary, batch stop on authentication/open breaker; one-day overlap replay |
| Position risk | Dummy book/entry prices; risk adjustment could undo notional limit | Persisted open book, actual stored close/ATR, combined sizing caps; serialized API check-and-create |
| Paper accounting | Wallet was independent of API-created positions and contained fake totals | Read-only projection of persistent open/closed positions; honest gross entry-cost basis |
| Validation | Invalid prices, quantities, stop direction and repeated closes | Validated entry model, conditional close update, entry switch, duplicate-symbol and portfolio caps |
| Monitor | Live candles in Parquet were not visible to SQLite-only monitor | Canonical Parquet first; legacy SQLite fallback retained |
| Scheduler | Explicit cron triggers could use host timezone; stale/off-session decision passes | Explicit Asia/Kolkata triggers and fresh, completed-bar gating for scheduled decision paths |
| API/UI | Legacy URLs and response shapes did not match backend | Versioned operating console, offline stored-symbol catalogue, explicit errors; unsupported API scheduling returns 501 |
| API concurrency | Synchronous pipeline work blocked the async event loop; force allowed overlap | Threadpool background execution and no force bypass within one API process |
| Access and secrets | Public bind default, token fragments in logs | Loopback defaults/ports, optional bearer middleware, no token-fragment logging, atomic token-file replacement |
| Deployment | Development frontend and misleading no-op deployment jobs | Compiled frontend behind unprivileged Nginx; real build jobs only; migrations included in Python image |

Broker message semantics were checked against [Kite WebSocket documentation](https://kite.trade/docs/connect/v3/websocket/) and the installed SDK parser; historical formatting against [Kite historical-data documentation](https://kite.trade/docs/connect/v3/historical/). SDK naive exchange datetimes represent host-local conversion of an epoch, so they are normalized using the host timezone, not unconditionally interpreted as IST.

## Verification

Run the reproducible checks from the repository root:

```powershell
python -m pytest tests -q --cov=trading_agent --cov-report=term-missing --cov-fail-under=45
python -m ruff check src tests migrations
python -m compileall -q src migrations
docker compose config --quiet
cd frontend
npm.cmd run build
```

The API integration suite uses temporary SQLite/Parquet roots and exercises position creation, duplicate rejection, limits, close, wallet/trades, auth, history, pipeline contracts, canonical candle monitoring, and Alembic upgrade/model alignment. Broker callbacks and historical responses are mocked; they are not proof of a live exchange session. Storage tests cover replay, mixed schemas, invalid data/paths, concurrent writers, and failed compaction. CI's existing 45% coverage gate is retained, not lowered.

Validation evidence (2026-10-19):

- Windows Python 3.13: **194 tests passed**, **56.30% measured coverage**, no warnings in the final run; fresh-database Alembic upgrade/check included.
- Ruff correctness checks, Python compilation, CLI help, TypeScript check and frontend production build passed.
- Both Python 3.11 and static-frontend Docker images built. An isolated no-egress Compose stack passed migrations, mixed-format Parquet replay/compaction, Nginx proxy, unauthorized-request rejection, browser token entry, paper open/close, reload, and persisted wallet/trade checks.
- The final rebuilt image also passed missing-open-interest and break-even-accounting checks. Its newer Starlette emitted a non-failing deprecation warning for the `httpx` test-client adapter; dependency locking and adapter migration remain follow-up work.
- The isolated smoke test found a standalone image directory-permission issue; runtime directories are now created for the non-root user.
- npm's initial audit reported one high-severity `source-map-js` advisory. The lockfile was updated from 1.2.1 to 1.2.2; the subsequent install audit reported zero vulnerabilities. See [the advisory](https://github.com/advisories/GHSA-68fv-2mgg-jv7q). This is not a complete Python/OS vulnerability assessment.
- No current broker login/backfill, real-data rewrite, full market-session test, or live order execution was performed by this audit.

These results apply to the working tree and dependencies actually tested, not every possible version allowed by the dependency ranges. The Python suite is not a full test of every research model. Container runtime smoke checks complement, but do not replace, the host suite.

To repeat the isolated browser check after building both images:

```bash
docker compose -f docker-compose.smoke.yml -p trading-agent-audit up --abort-on-container-exit --exit-code-from smoke
docker compose -f docker-compose.smoke.yml -p trading-agent-audit down
```

The separate test stack uses no `.env`, host ports, or real-data mounts and has no external network access. It records a synthetic paper trade only inside its disposable container. Always remove it afterward so the next run starts with an empty test ledger. CI runs this check before publishing the backend image.

## Deployment constraints and recovery

- Run one API worker, one ticker, one scheduler, and at most one historical download job. Pipeline state and historical pacing are process-local; there is no cluster-wide job lock or distributed rate limiter.
- SQLite is the supported relational store. File locks assume a local filesystem with working OS locks; network-share semantics are not validated.
- Back up `data/` and `models/` with writers stopped, protect the token separately, and test restore into an isolated directory before a release. No automated restore drill or disaster-recovery target is established.
- Storage reads currently scan the symbol/interval files. Physical row counts include replay copies. Establish a tested compaction cadence and capacity plan before large-scale unattended use.
- Catch-up replays a one-day overlap and the trailing gap. It cannot certify every missing historical candle. Older overwritten data needs an explicit re-download after a backup; no automatic destructive rebuild is performed.
- Live bars at initial connection/reconnection can be partial; the final bar may await another tick. The post-close historical replay is necessary. Do not treat live-derived volumes as exchange-certified until reconciled.
- The daily news cleanup remains a logged placeholder; scheduler validation output and sentiment persistence need further integration. A heartbeat is feed evidence, not confirmation every symbol is current.
- Position monitoring is advisory. It is not an order/fill engine; legacy monitoring conventions and legacy SQLite timestamps still require migration. Manual entry/exit values are user-supplied and are not broker-validated prices.
- Stop/target evaluation from OHLC cannot determine actual intrabar fill sequence or executable prices. Gross paper accounting excludes fees, slippage and unrealized returns.
- After changing code, build images and recreate the selected services. Do not delete data or prune all Docker images to deploy a fix.

## Gates before institutional or unattended live use

These are requirements, not implemented features or compliance claims:

1. **Execution and reconciliation:** approved broker order adapter; idempotent order intents; durable order/fill state; reconnect replay; broker positions/cash reconciliation; partial fills/rejections/cancel races; human override and tested kill switch. No live order path currently exists.
2. **Financial controls:** immutable double-entry ledger, decimal money, deposits/withdrawals, charges/taxes, corporate actions, short/margin constraints, mark-to-market, daily-loss/drawdown and sector/correlation limits enforced at execution. Current API caps cover only paper notional/stop risk and count.
3. **Data quality:** official exchange holiday/special-session calendar, per-symbol quality/freshness watermarks, complete interior-gap detection, corporate-action/symbol history, deterministic corrections and replay ordering across replicas. Weekday/time guards are not an exchange calendar.
4. **Security/governance:** TLS gateway, users/RBAC, scoped service identities, secret manager/rotation, immutable audit trail, dependency/SBOM vulnerability review, signed images and appropriate regulatory/data-licensing review. One shared bearer token is not enterprise identity management.
5. **Reliability:** durable job queue, distributed coordination, bounded resource consumption, load/failure/soak tests, metrics and alerts, backup/restore drills, failover design and agreed recovery objectives. Run a complete market session and forced-disconnect exercise before promotion.
6. **Model/research governance:** independently verified no-lookahead replay, point-in-time universes, model/artifact provenance, controlled promotion/rollback, out-of-sample performance and execution-cost evidence. Research results and LLM outputs are not trading authorization.
7. **Reproducibility and coverage:** locked Python dependencies, pinned base-image digests, browser coverage beyond the basic paper workflow, broader critical-path coverage and cross-process/race tests. A passing baseline is necessary but insufficient.

Until these gates have evidence and an owner, keep this deployment local/private and paper-only.
