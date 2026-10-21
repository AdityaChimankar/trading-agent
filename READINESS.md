# Integration audit and readiness

Reviewed: 2026-10-21. Scope: local-first research and paper operations, not approval for live broker execution. Existing user data, positions, credentials, and unrelated research edits were preserved. No broker orders are part of this work.

## Operating boundary

### Latest: gemini repair with the rework overview (2026-10-21)

The Find view again exposes paper-book equity/cash, open positions, quick symbol selection, paired bullish/bearish cards and cached headlines. Advanced scan and evidence details are collapsible. Find / Inspect / Validate / Paper still share selection and require current settings, unexpired plans and explicit actions. Fourteen unreachable legacy frontend files were removed; the old UI remains recoverable from Git and `rework`.

Paper entry creation now applies configurable single-position exposure, realized daily-loss and realized drawdown limits inside the same serialized SQLite transaction as the insert. Invalid ledger evidence blocks new entries while exits remain available. Scan allocations bind to the ledger, session date and risk settings; changed or unreadable state zeros allocations on reads. Unreadable outcome evidence removes qualification without failing the entire scan response.

Readiness and automatic research API routes are connected. Automation starts disabled, observes local stored candles only after explicit enabling, shares the manual scan lock, and requires one API worker. Startup/shutdown closes resources on failures; an old worker still finishing storage I/O must exit before an in-process restart can succeed. Strategy Lab now exposes bounded causal profile/baseline comparisons using the shared bracket replay, chronological windows, equal-notional additive statistics and unavailable metrics represented as null. It cannot validate or promote a model.

CI checks now include `gemini` and `rework`; image publishing remains limited to `main` and `develop`. Setup and branch/data compatibility guidance are in the README and user guide.

Verification: **404 Python tests passed**, with **65.45% measured coverage** under the existing configuration (which excludes research/script modules from the percentage; Strategy Lab and replay have explicit tests). Ruff, Python compilation, the TypeScript/Vite production build, and both offline browser scripts passed. Desktop/mobile captures were inspected; workflow checks cover the overview, settings/evidence gates, ledger invalidation, expiry, shared selection, explicit actions and overflow. Compose configuration validated with `.env.example` substituted for the private env file. Screenshots and the final test log are local artifacts under `artifacts/dashboard-repair/` and `artifacts/repair-pytest.log`. The installed Starlette test-client deprecation and SciPy solver-option warnings remain non-failing; the existing lazy Plotly bundle still triggers Vite's size warning.

This repair changes source in an isolated `gemini` worktree. The original `rework` checkout, `.env`, real database, candle history and model artifacts were preserved. No Docker services were rebuilt/restarted, no external provider or broker session was exercised, and no remote publication was performed. Realized gross paper limits do not account for unrealized losses or actual fees; strategy comparisons do not establish profitability. Prior dated verification entries below describe their respective historical revisions.

### Earlier: coordinated research UI (2026-10-20)

The research dashboard now has Find / Inspect / Validate / Paper steps with shared symbol and interval, global scan settings, per-input evaluation result caches, and explicit rule/promoted-ML policy handoffs. Paper operations no longer embeds a second independent scanner. Validation, news and paper-book changes mark current workspace scan plans for recomputation. Changing selection during a request does not attach its result to another symbol. Unsubmitted manual paper price fields reset when the symbol changes.

No backend strategy, model artifact, broker session or promotion gate was changed for this UI update. Walk-forward parameters remain experimental; no automatic parameter deployment or model promotion is implied. The manual paper book is distinct from prospective model confirmation. Browser-session evaluation caches are not durable across reloads.

Verification: all 295 Python tests passed (62.12% measured coverage), Ruff passed, and the TypeScript/Vite production build, authentication browser smoke check, and mocked browser workflow checks passed. Workflow checks cover request inputs, cross-symbol in-flight results, input-keyed caching, evaluation errors, scan invalidation, paper selection, explicit ML policy handoff, expired plans and mobile overflow. Desktop/mobile screenshots were inspected. The existing lazy Plotly bundle and Windows physical-core discovery warnings remain non-failing. The legacy-model SHA-256 is unchanged. Docker images/services have not been rebuilt or restarted for this UI update.

### Separate BUY/SELL outcome lifecycle

Implemented a shared 13-feature training/inference contract; four global-session partitions (50/15/15/20); full label-horizon purging; independent calibrators; threshold selection confined to tuning; checksum/source-bound candidate artifacts; durable non-reusable test reservations; prospective shadow-paper recording/settlement; and explicit promotion requiring both historical and forward gates. No future outcome can be supplied through an API or CLI override. The local SQLite/joblib store is trusted, not a tamper-proof enterprise audit system.

Trained candidate `9b5e7dc3b1984e38836f1ba1b0b807fb` on 2026-10-20 from 20,000 minute candles each for HDFCBANK, ICICIBANK, INFY, RELIANCE and TCS. Its historical evaluation window starts at 2026-09-20 18:30 UTC and data ends 2026-10-19 04:44 UTC. Both tuning directions failed the declared threshold gates; the frozen policy abstained on all test candles. **Status: historical_gate_failed. Test trades: zero; test win rates: unavailable. No promotion or forward confirmation is claimed.**

The first candidate's artifact SHA-256 is `574cbf54e2d8bec4856a90e3edb82a1b107affe9ee7febf7e3796d235647952e`. The legacy model remains unchanged. The lifecycle requires at least 100 selected historical trades/10 sessions and 100 forward trades/20 sessions **per side**, >60% observed wins with both Wilson and session-bootstrap lower win bounds above 60%, plus positive bootstrap lower net expectancy. This is a release rule, not evidence those requirements have been achieved.

Verification: 295 tests passed with 62.12% measured coverage, including vector/scalar fill parity, test-price perturbation isolation, frozen holdout reservation, artifact checksums, no retroactive paper entries, missing-outcome rejection, promotion without forward evidence rejected, and shared dashboard probabilities. Coverage excludes the research package from its percentage; replay is explicitly tested. Ruff, the TypeScript/Vite production build, browser smoke checks, and the candidate-status API check passed. The Windows physical-core discovery fallback and large lazy-loaded Plotly chunk emit non-failing warnings; thread limits are applied during training. Source changes are not yet rebuilt/deployed to Docker, and a full market-session observer run remains outstanding.

### Earlier rule-validation audit

Outcome-validation update (2026-10-20): fixed-rule next-open bracket replay, costs/slippage, conservative ambiguous/gap fills, session exits, independent calibration/holdout partitions, per-direction sample/Brier/expectancy gates and bounded evidence expiry are implemented. The dashboard defaults to requiring qualified evidence. The legacy hybrid artifact still cannot supply validated win probabilities; no retraining/promotion occurred. Freshness and session eligibility are rechecked at scan completion and on reads; thin turnover and recent missing-bar candidates are blocked.

A read-only local RELIANCE minute replay using the new default bracket policy returned **insufficient evidence**, not a qualified pick: 45 holdout trades, 37.78% historical wins, mean net trade return **-0.1024%** under the stated costs. BUY had 10 holdout trades and SELL 35; both failed minimum sample requirements. This is one stored-data diagnostic, not a universe performance estimate or investment recommendation. No evidence file or model was promoted by that check.

Training code now excludes unknown-future labels, uses globally chronological multi-symbol splits with label-horizon purging, separates tuning from reported holdout, and uses causal rolling regime ranks. These are code-level safeguards, not proof of a profitable model. Incremental live scanning, point-in-time universes, multiplicity control, market/sector feature validation, independent hybrid training and forward-paper track records remain outstanding.

Verification for this update: **280 tests passed**, **61.22% measured coverage**, Ruff passed, and the TypeScript/Vite production build passed. Coverage configuration excludes the research package from its percentage, although bracket replay is explicitly tested. New tests cover conservative fills, costs, session cutoffs, insufficient/expired evidence, timestamp purging, causal regime ranks, and separation of tuning from holdout in both trainers. The large lazy-loaded Plotly chunk still produces a non-failing build warning.

The active model SHA-256 remains `B597138EFC0D93077392C5F26F0391DCEE9398E60CD8124F900E386E46B9B40B`. Docker images/services were **not rebuilt or restarted for this update**. The isolated browser-smoke script was extended for the new panel and insufficient-data response but has not been rerun against rebuilt images; older Docker evidence below applies to the prior restoration only.

The supported path is Kite historical/live data -> Parquet -> research scan/risk plans -> `/api/v1/research` -> `ResearchDashboard.tsx`, with the persistent SQLite paper book available through the Paper operations tab (`OperatingConsole.tsx`). Original Plotly rendering and layout styles are reused. Unmounted legacy components were subsequently removed in the 2026-10-21 repair; their historical source remains in Git. Incompatible `/api` routes are not silently reintroduced.

Restoration (2026-10-20): recovered the original layout and nine-feature classifier contract from Git commit `5dcea32`. Full-watchlist background scans now use real rules plus model predictions, not constant placeholder scores. Conflicts result in HOLD; missing inputs remain unavailable. Cached, explicitly requested LLM news sentiment can modify rankings when time-aligned. Bullish/bearish plans share basket risk/exposure reservations; stale and held symbols are blocked. The artifact lacks training provenance/interval metadata: research inference is restored, but model performance/promotion is not validated. The older `/opportunities/{symbol}/decision` compatibility endpoint remains rule-only; the main dashboard uses `/research`.

Prior simple-panel baseline: **212 Python tests passed** on Windows and an isolated Docker browser smoke passed. This is historical evidence, not evidence that the restored model has predictive edge. New restoration tests cover feature causality, class mapping, missing/incompatible models, conflict/abstention handling, completed-candle filtering, full-universe coverage, news timestamps and shared basket limits. External LLM calls require separate provider validation.

Walk-forward restoration: the old optimizer attempted to unpack a dataclass as a dict, searched RSI parameters unused by its signals, allowed training labels beyond the training boundary, and used inconsistent train/test entry/cost calculations. The corrected rule-only evaluator searches effective thresholds, purges forward outcomes, uses non-overlapping positions and consistent costs, and exposes baseline comparisons plus skipped-fold reasons. The dashboard action is bounded and explicit, with no model training/promotion or order placement. Tests cover future-price perturbation, train/test boundaries, next-open fills, costs, baseline parity, API bounds and concurrent-request rejection.

Real stored-data checks (2026-10-20):

- Full watchlist: 2,319 processed; 2,266 evaluated; 53 skipped for insufficient/invalid data; zero processing errors. All 2,266 evaluated rows were stale, so zero fresh eligible plans. This is coverage evidence, not a market-live validation.
- RELIANCE minute rule walk-forward: 20,000 candles, 21 evaluated folds, 3,750/750 train/test windows and assumed 5 bps round-trip costs. Optimized: 297 trades, mean net trade return -0.0447%; default: 631 trades, -0.0446%. Both additive net-return sums were negative (-13.277% and -28.1261%). Different trade counts make aggregate sums insufficient for declaring improvement. These are not portfolio returns or validation of the ML model.
- No broker data download, live order, ML retraining, model overwrite or paid LLM request was needed for these checks. News-provider response handling is tested with mocked transport, not a verified external provider session.

Current host validation: **258 tests passed**, **58.61% measured coverage** under the existing coverage configuration (which excludes some research/script modules), Ruff correctness checks passed, Python compilation passed, and TypeScript/Vite production build passed. Six mocked news-provider tests verify bounded headlines, opt-in costs, invalid-output rejection without cache overwrite, client cleanup, and timestamp filtering. The 16 optimizer tests are included in the passing suite even though the existing coverage configuration omits the research directory. Three added model-portability tests cover the Windows Cython alias, version-mismatch rejection and refusal to remap unrelated missing modules. The lazy-loaded Plotly chunk still exceeds Vite's size-warning threshold.

Docker integration uncovered a real legacy-model portability failure: the Windows-trained artifact referenced `_loss` and embedded scikit-learn 1.5.2, while the unpinned Linux build installed 1.9.1. Dependencies now pin 1.5.2 and the trusted local loader resolves only that known Cython module alias for that release. Cross-version estimator warnings are treated as incompatibility, not ignored. A disposable Linux test using 1.5.2 reproduced the host RELIANCE probability (0.39045254141264163; below the 0.55 confidence threshold, hence HOLD). This is a sample inference compatibility check, not a full portability or performance certification. The model file was not rewritten. Source-only Docker edits now reuse separately cached dependencies/browser layers.

Final rebuilt-image checks passed: 100 stored symbols (alphabetically first 100 evaluated rows from the saved Windows scan) retained identical candle timestamps and ML actions in Docker; maximum confidence difference was 1.11e-16. This validates that sample, not all future models/data. The isolated no-egress browser smoke also passed the Windows-loss pickle fixture, real synthetic-data Plotly rendering, walk-forward API fold calculations and insufficient-history UI, authentication, migrations, Parquet replay/compaction, and paper open/close/reload persistence. Its disposable containers/network were removed afterward; real candle/model mounts were never attached to the smoke stack.

Deployment verification: the rebuilt `api` and `frontend` services were recreated with existing data/model mounts preserved. API health, frontend and Nginx readiness proxy returned HTTP 200; the new frontend bundle was served. Deployed endpoints reported 2,319 configured/processed symbols, available RELIANCE ML inference, and a completed 21-fold RELIANCE rule walk-forward. SHA-256 of the original model remained `b597138efc0d93077392c5f26f0391dcee9398e60cd8124f900e386e46b9b40b`. Ticker/scheduler live services were not started.

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
