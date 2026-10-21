import { Suspense, lazy } from 'react'

import {
  useChart,
  useLlmSignals,
  useMlSignals,
  useNews,
  useOpenPosition,
  usePatterns,
  useSignals,
  useSizing,
} from '../api/queries'
import { actionClass, cx, inr, num, shortTime } from '../lib/format'
import { DataTable, Metric, Panel, StateMessage, type Column } from './ui'

const PriceChart = lazy(() => import('./PriceChart'))
import type { Action, LlmSignalRow, MlSignalRow, NewsRow, SignalRow } from '../api/types'

export default function SymbolDetail({
  symbol,
  capital,
}: {
  symbol: string | null
  capital: number
}) {
  const chart = useChart(symbol)
  const patterns = usePatterns(symbol)
  const sizing = useSizing(symbol, capital)
  const news = useNews(symbol)
  const signals = useSignals(symbol)
  const llm = useLlmSignals(symbol)
  const ml = useMlSignals(symbol)
  const openPosition = useOpenPosition()

  if (!symbol) {
    return (
      <Panel title="Symbol Detail">
        <p className="muted small">Select a symbol from the sidebar or the symbol picker to begin.</p>
      </Panel>
    )
  }

  const sizingData = sizing.data
  const adjusted = sizingData?.has_signal ? sizingData.adjusted : null
  const plan = adjusted?.base_plan ?? null
  const canRecord = Boolean(adjusted && !adjusted.blocked && adjusted.approved_size > 0)

  return (
    <>
      <Panel
        title={`${symbol} — Price, Indicators & Patterns`}
        actions={chart.data?.insufficient_data === false ? (
          <div className="metric-row compact">
            <Metric label="RSI" value={num(chart.data.latest.rsi, 1)} />
            <Metric label="ADX" value={num(chart.data.latest.adx, 1)} />
            <Metric label="ATR" value={num(chart.data.latest.atr, 2)} />
          </div>
        ) : undefined}
      >
        <StateMessage
          loading={chart.isLoading}
          error={chart.error}
          empty="Chart unavailable."
          isEmpty={false}
        />
        {chart.data && (
          <Suspense fallback={<p className="muted small">Loading chart…</p>}>
            <PriceChart data={chart.data} />
          </Suspense>
        )}
      </Panel>

      <Panel
        title="Position Sizing"
        subtitle="ATR-based, portfolio-adjusted for correlation and total risk budget."
      >
        <StateMessage
          loading={sizing.isLoading}
          error={sizing.error}
          empty="No active BUY/SELL signal for this symbol — nothing to size."
          isEmpty={sizingData?.has_signal === false}
        />

        {openPosition.isError && (
          <p className="error small">
            Could not record position:{' '}
            {openPosition.error instanceof Error ? openPosition.error.message : 'unknown error'}
          </p>
        )}
        {openPosition.isSuccess && (
          <p className="small agree">
            Recorded {openPosition.data.shares} shares ({inr(openPosition.data.value)}) for {symbol}.
          </p>
        )}

        {plan && sizingData?.has_signal && (
          <>
            <div className="metric-row">
              <Metric label="Action" value={<span className={actionClass(sizingData.action)}>{sizingData.action}</span>} />
              <Metric label="Stop-loss" value={num(plan.stop_loss, 2)} />
              <Metric label="Take-profit" value={num(plan.take_profit, 2)} />
              <Metric
                label="Approved size"
                value={`${adjusted?.approved_size ?? 0} sh`}
                hint={
                  adjusted && adjusted.approved_size !== plan.position_size
                    ? `${adjusted.approved_size - plan.position_size} vs per-trade-only`
                    : undefined
                }
              />
              <Metric label="Approved value" value={inr(adjusted?.approved_value)} />
            </div>

            {adjusted?.blocked ? (
              <p className="warn-text small">Blocked: {adjusted.block_reason}</p>
            ) : adjusted && adjusted.approved_size < plan.position_size ? (
              <p className="small muted">
                Reduced from {plan.position_size} to {adjusted.approved_size} shares — total risk budget{' '}
                {num(adjusted.total_risk_used_pct, 1)}% used
                {adjusted.correlated_with.length > 0
                  ? `, correlated with ${adjusted.correlated_with.join(', ')} (cluster at ${num(
                    adjusted.cluster_exposure_pct,
                    1,
                  )}%)`
                  : ''}
              </p>
            ) : (
              <p className="muted small">
                Risking {inr(plan.risk_amount)} · portfolio risk budget{' '}
                {num(adjusted?.total_risk_used_pct, 1)}% used before this trade
              </p>
            )}

            <button
              type="button"
              className="btn btn-primary"
              disabled={!canRecord || openPosition.isPending}
              onClick={() =>
                openPosition.mutate({ symbol, action: sizingData.action as 'BUY' | 'SELL', capital })
              }
            >
              Record as open position ({adjusted?.approved_size ?? 0} shares)
            </button>
          </>
        )}
        <p className="muted small">This is a calculated plan, not an order — nothing is placed automatically.</p>
      </Panel>

      <div className="two-col">
        <Panel
          title="Detected Chart Patterns"
          subtitle="Pattern-based bias detected on the current candle window."
          actions={patterns.data?.bias ? (
            <span className={cx('tag', patterns.data.bias === 'bullish' ? 'tag-buy' : patterns.data.bias === 'bearish' ? 'tag-sell' : '')}>
              {patterns.data.bias.toUpperCase()}
            </span>
          ) : undefined}
        >
          <StateMessage
            loading={patterns.isLoading}
            error={patterns.error}
            empty="No patterns detected in the current window."
            isEmpty={!patterns.data || patterns.data.patterns.length === 0}
          />
          {patterns.data && patterns.data.patterns.length > 0 && (
            <ul className="pattern-list">
              {patterns.data.patterns.map((p) => (
                <li key={p} className={cx('small', patterns.data!.bias === 'bullish' ? 'pnl-pos' : patterns.data!.bias === 'bearish' ? 'pnl-neg' : 'muted')}>
                  {patterns.data!.bias === 'bullish' ? '🟢 ' : patterns.data!.bias === 'bearish' ? '🔴 ' : '⚪ '}{p}
                </li>
              ))}
            </ul>
          )}
        </Panel>

        <Panel title="Recent News + Sentiment">
          <StateMessage
            loading={news.isLoading}
            error={news.error}
            empty="No tagged news for this symbol yet."
            isEmpty={(news.data ?? []).length === 0}
          />
          <DataTable<NewsRow>
            rows={news.data ?? []}
            columns={NEWS_COLUMNS}
          />
        </Panel>
      </div>

      <Panel title="Signal History (Rule-Based)" collapsible>
        <StateMessage
          loading={signals.isLoading}
          error={signals.error}
          empty="The decision agent hasn't logged any signals yet."
          isEmpty={(signals.data ?? []).length === 0}
        />
        <DataTable<SignalRow> rows={signals.data ?? []} columns={SIGNAL_COLUMNS} maxRows={10} maxHeight={300} />
      </Panel>

      <div className="two-col">
        <AgentPanel<LlmSignalRow>
          title="LLM Agent vs Rule-Based"
          agreement={llm.data?.agreement_pct ?? null}
          rows={llm.data?.rows ?? []}
          loading={llm.isLoading}
          error={llm.error}
          empty="LLM decision agent hasn't run yet for this symbol."
          actionHeader="LLM"
          getAction={(r) => r.llm_action}
          getRationale={(r) => r.rationale}
          defaultCollapsed={false}
          maxRows={10}
          maxHeight={300}
        />
        <AgentPanel<MlSignalRow>
          title="ML Model vs Rule-Based"
          agreement={ml.data?.agreement_pct ?? null}
          rows={ml.data?.rows ?? []}
          loading={ml.isLoading}
          error={ml.error}
          empty="ML decision agent hasn't run yet (train a model with train_ml_model.py)."
          actionHeader="ML"
          getAction={(r) => r.ml_action}
          defaultCollapsed={true}
          maxRows={10}
          maxHeight={300}
        />
      </div>
    </>
  )
}

const NEWS_COLUMNS: Column<NewsRow>[] = [
  { header: 'Headline', cell: (r) => r.headline },
  { header: 'Source', cell: (r) => r.source ?? '—' },
  {
    header: 'Score',
    align: 'right',
    cell: (r) => (
      <span className={cx('mono', r.score == null ? undefined : r.score >= 0 ? 'pnl-pos' : 'pnl-neg')}>
        {num(r.score, 2)}
      </span>
    ),
  },
  { header: 'Published', align: 'right', cell: (r) => shortTime(r.published_at) },
]

const SIGNAL_COLUMNS: Column<SignalRow>[] = [
  { header: 'Time', cell: (r) => shortTime(r.timestamp) },
  { header: 'Action', cell: (r) => <span className={actionClass(r.action)}>{r.action}</span> },
  { header: 'RSI', align: 'right', cell: (r) => num(r.rsi, 1) },
  { header: 'ADX', align: 'right', cell: (r) => num(r.adx, 1) },
  { header: 'Sentiment', align: 'right', cell: (r) => num(r.sentiment_score, 2) },
  { header: 'Rationale', cell: (r) => <span className="muted">{r.rationale}</span> },
]

interface AgentRow {
  timestamp: string
  rule_based_action: Action | null
  confidence: number | null
}

function AgentPanel<T extends AgentRow>({
  title,
  agreement,
  rows,
  loading,
  error,
  empty,
  actionHeader,
  getAction,
  getRationale,
  defaultCollapsed = false,
  maxRows = 10,
  maxHeight = 300,
}: {
  title: string
  agreement: number | null
  rows: T[]
  loading: boolean
  error: unknown
  empty: string
  actionHeader: string
  getAction: (row: T) => Action
  getRationale?: (row: T) => string | null
  defaultCollapsed?: boolean
  maxRows?: number
  maxHeight?: number
}) {
  const columns: Column<T>[] = [
    { header: 'Time', cell: (r) => shortTime(r.timestamp) },
    {
      header: actionHeader,
      cell: (r) => {
        const action = getAction(r)
        return <span className={actionClass(action)}>{action}</span>
      },
    },
    {
      header: 'Rules',
      cell: (r) => (
        <span className={actionClass(r.rule_based_action)}>{r.rule_based_action ?? '—'}</span>
      ),
    },
    { header: 'Confidence', align: 'right', cell: (r) => num(r.confidence, 2) },
    ...(getRationale
      ? [{ header: 'Rationale', cell: (r: T) => <span className="muted">{getRationale(r)}</span> }]
      : []),
  ]

  return (
    <Panel title={title} collapsible defaultCollapsed={defaultCollapsed}>
      {agreement !== null && <Metric label="Agreement rate (last 20)" value={`${agreement}%`} />}
      <StateMessage loading={loading} error={error} empty={empty} isEmpty={rows.length === 0} />
      <DataTable<T> rows={rows} columns={columns} maxRows={maxRows} maxHeight={maxHeight} />
    </Panel>
  )
}
