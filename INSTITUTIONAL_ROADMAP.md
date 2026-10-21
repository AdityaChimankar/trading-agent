# Trading Agent: audit and staged institutional roadmap

Reviewed 2026-10-21. This is an engineering roadmap, **not certification of live-trading readiness or profitability**. The supported application remains local research and manual paper trading. Existing ingestion, full-universe Opportunity Finder, hybrid decisions, Plotly charts, LLM news, sizing, walk-forward evaluation and BUY/SELL outcome lifecycle remain available.

## Changes delivered in this audit

| Gap found | Implemented change | Remaining boundary |
|---|---|---|
| Research scans needed repeated manual starts | Opt-in full-universe candle/ledger change detector, single-scan coordination, cooldown, bounded retries, operator pause and dynamic chart reads | Research only; resets off on API restart. Full-pass performance, exchange calendars and durable distributed scheduling remain unresolved |
| Old scan sizing could survive a book change in another client | Server binds allocations to a fingerprint of the ledger, risk configuration and IST session date; mismatch blocks allocation until re-scan | This is not a reserved or executable broker order |
| Entry API lacked realized daily-loss/drawdown and single-name controls | Serialized SQLite entry check adds these gates; scans use current capital and matching single-name cap | Realized gross equity only; unrealized losses and actual execution costs remain unmodeled |
| Monitor could evaluate pre-entry or incomplete bar extremes | Requires trustworthy entry time and completed post-entry candles; filters old/future reversal signals | Advisory monitoring does not replay every missed bar or execute a stop |
| Monitor alert deduplication depended on process memory and polling time | Persisted position/candle/type identity under a SQLite write transaction | No enterprise notification delivery or immutable event ledger |
| Legacy strategy lab used same-close entries, overlapping trades and optimistic compounding/annualization | Canonical next-open bracket replay, non-overlapping per-symbol trades, explicit costs and additive statistics; undefined metrics stay unavailable | Research statistics are not portfolio returns, pristine holdout results or demonstrated edge |
| Legacy `verify-fast` returned success without comparing a reference | Actual scalar/vector comparison and a regression test that injects disagreement; default parameter type mismatch fixed | Legacy gross fixed-horizon diagnostics still are not the conservative Strategy Research Lab or portfolio validation |
| Health was scattered across views | Operations overview distinguishes interval directory coverage, scan freshness, model governance, feed and entry blockers | Directory presence is not validation of all candle data |
| Strategy alternatives were hard to compare fairly | Fixed baseline, trend-confirmed and liquidity-confirmed hypotheses share one chronological exploratory slice and one execution policy | No automatic threshold search, scanner replacement or promotion |

## How to use the improved workflow

1. Review **Operations overview**. Investigate the highest-priority blockers; data recovery remains explicit.
2. In **Find**, set interval, mode, capital and risk policy, then scan. A configured symbol count is not a fresh-data coverage claim.
3. In **Inspect**, review the exact candle, opposing evidence, stop, target, size and plan expiry.
4. In **Validate**, compare fixed profiles in the **Strategy Research Lab**, or run the existing Walk-Forward Optimizer. The tools test different policies and do not apply their outputs to the scanner.
5. Use rule outcome validation or the separate model qualification process before any evidence-gated plan. Do not reuse the already reserved ML test period to search for a favorable result.
6. Use **Paper review** for manual records. The separate prospective outcome-model observer is still required for model promotion.

The first outcome-model candidate remains failed. This audit did not train or promote a replacement, download market data, change credentials, or demonstrate a higher winning rate.

## Remaining gaps and release criteria

### Priority 0: required before considering live execution

- **Broker order lifecycle:** durable client intent IDs, idempotent submission/recovery, partial fills, rejects, cancellations, disconnected/reordered updates, reconciliation with broker order/trade/position records and emergency shutdown. Acceptance: fault-injected sandbox/paper scenarios produce no duplicated order or unexplained position.
- **Account truth:** actual fees/taxes/slippage, realized and unrealized P&L, cash/settlement, short-side financing/borrow rules and trustworthy marks. Acceptance: daily reconciliation to broker statements; missing/stale marks block risk authorization.
- **Market-data truth:** exchange calendars including holidays/special sessions, instrument identity/renames/delistings, corporate actions, continuity and point-in-time universes. Acceptance: explicit source/version/quality evidence and deterministic replay for every decision.
- **Exposure controls:** sector, correlated positions, strategy/venue risk, concentration and stress scenarios. Current realized-loss gates are not portfolio stress management.
- **Security and operations:** enforced deployment-specific authentication, roles/approval boundaries, secret rotation, immutable audit records, backups with restore drills, durable job queue, process supervision and alert acknowledgement. Current bearer-token/loopback safeguards and SQLite locks are a local foundation only.

### Priority 1: required before calling a strategy profitable

- Register each hypothesis and its policy before evaluating new data.
- Compare baseline and alternatives under the same fills, costs, session treatment and eligible universe; report both rejected and accepted observations.
- Use chronological, horizon-purged partitions; choose thresholds only on training/validation data. Keep a genuinely new test period and account for multiple comparisons.
- Evaluate performance by side, symbol, session, liquidity, volatility and market regime. Report sample size, uncertainty, net expectancy, drawdown and turnover—not win rate alone.
- Obtain prospective shadow/paper evidence with real-time timestamps, frozen artifacts, no retroactive entries and documented execution differences.
- Promote only through the existing explicit gates; reject expired or mismatched evidence. Passing them does not guarantee future returns.

### Priority 2: capacity and user experience

- Incremental streaming scans rather than full-universe history reads. Automatic triggering is now available, but `read_latest(n)` still loads/deduplicates the full stored history before tail selection; it is not bounded to reading 20,000 rows. File fragmentation can exhaust observation budgets. Acceptance: measured p95 latency fits the intended entry window, without losing coverage or silently skipping failures.
- Versioned decision snapshots, saved research workspaces and per-user permissions.
- Dedicated portfolio attribution and data-quality drill-downs; paging for large ledgers.
- Cross-platform builds and failure-injection integration tests beyond the current local/synthetic coverage.

## Research assumptions, not strategy recommendations

Trend confirmation and relative-volume filters are **hypotheses**. They can discard both bad and good trades and may reduce sample size. No profile is labeled more profitable until independent evidence supports it. The lab uses a fixed 1.5 ATR stop, 2:1 target, at most 24 bars, 10 bps round-trip costs and 2 bps adverse slippage per fill. These cost values are assumptions, not broker fee quotes.

The revised `optimize strategy-lab` command reports additive net trade return/drawdown, not compounded portfolio performance. Its old fixed-INR `commission` argument is rejected when nonzero because such costs require a quantity/allocation model; use the declared `cost_bps`. Annualized Sharpe and raw pre-slippage expectancy are unavailable rather than fabricated.

## Primary references behind the review

- [scikit-learn: cross-validation and model evaluation](https://scikit-learn.org/stable/modules/cross_validation.html) explains why evaluation on data used for fitting/selection is not independent performance evidence.
- [Kite Connect order API](https://kite.trade/docs/connect/v3/orders/) and [postbacks](https://kite.trade/docs/connect/v3/postbacks/) describe asynchronous order-state changes, including partial fills. A broker-connected execution system needs to handle these states; this application does not yet implement that lifecycle.

These references inform the engineering gaps above. They do not validate this application's trading performance or regulatory compliance.
