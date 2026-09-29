import { usePipeline } from '../api/queries'
import type { AgentSignal } from '../api/types'
import { actionClass, inr, num } from '../lib/format'
import { DataTable, Metric, Panel, StateMessage } from './ui'

// The wiring below is not decoration - each box names the module that really
// does that step, so the diagram can be checked against the code.
const STAGES: { title: string; module: string; note: string }[] = [
  { title: '1 · Candle', module: 'storage/candles', note: 'One minute bar per symbol, the shared input for every path.' },
  { title: '2 · Three paths, run in parallel', module: 'strategy/*', note: 'Rule, ML and LLM each decide independently and write to their own table.' },
  { title: '3 · Conviction', module: 'analysis/opportunity_finder.py', note: 'Agreement ADDS points, disagreement SUBTRACTS them. Still not a decision.' },
  { title: '4 · Position sizing', module: 'core/position_sizing.py', note: 'Turns one action into shares: risk 1% of capital, stop at 1.5×ATR.' },
  { title: '5 · Portfolio limits', module: 'risk/portfolio_risk.py', note: 'Shrinks or blocks the plan against what is already open. The real size.' },
  { title: '6 · Open position', module: 'storage/open_positions', note: 'The only stage that changes your book — and it is always manual.' },
]

function AgentCell({ agent }: { agent: AgentSignal | null }) {
  if (!agent) return <span className="muted small">no row yet</span>

  return (
    <span>
      <span className={actionClass(agent.action)}>{agent.action}</span>
      {agent.confidence !== null && (
        <span className="muted small"> {num(agent.confidence * 100, 0)}%</span>
      )}
      {' '}
      {!agent.is_call ? (
        // A HOLD is an abstention, not a disagreement - showing "✗ rules"
        // here would read as the agents fighting when they simply sat out.
        <span className="muted small">(sat out)</span>
      ) : agent.rule_based_action === null ? (
        // Older rows predate the rule_based_action column, so agreement
        // cannot be measured for them - claiming DISAGREE would be a lie.
        <span className="muted small">(no same-cycle rules call)</span>
      ) : agent.agrees_with_rule ? (
        <span className="agree small">✓ rules</span>
      ) : (
        <span className="disagree small">✗ rules</span>
      )}
    </span>
  )
}

export default function PipelinePage({
  capital,
  onSelectSymbol,
}: {
  capital: number
  onSelectSymbol: (symbol: string) => void
}) {
  const { data, isLoading, error } = usePipeline(capital)

  const s = data?.summary
  const rows = data?.rows ?? []

  return (
    <>
      <Panel
        title="🔗 Signal Pipeline"
        subtitle="How the rule-based agent, the ML model and position sizing are wired together — with the live numbers flowing through each link right now."
      >
        <ol className="flow">
          {STAGES.map((stage) => (
            <li key={stage.title} className="flow-node">
              <div className="flow-title">{stage.title}</div>
              <div className="flow-module mono small">{stage.module}</div>
              <p className="muted small">{stage.note}</p>
            </li>
          ))}
        </ol>

        <p className="muted small">
          The honest caveat: the ML and LLM calls are logged for COMPARISON only — they never
          place or size anything. Position size comes solely from the rule-based action, which
          is why a row below can show the ML disagreeing while the plan is still sized. That is
          the connection, not a bug. Agreement is measured over directional calls only: a HOLD
          is an abstention, so it is shown but never counted.
        </p>
      </Panel>

      <Panel
        title="Live agreement & sizing"
        subtitle="One row per symbol the ranking is currently sizing: what each path last said, whether they agree, and the rupee size the plan came out at."
      >
        <StateMessage
          loading={isLoading}
          error={error}
          empty="Nothing to size right now — no symbol is scoring bullish or bearish enough."
          isEmpty={rows.length === 0}
        />

        {s && rows.length > 0 && (
          <div className="metric-row">
            <Metric label="Symbols sized" value={num(s.sized, 0)} hint={`of ${s.symbols} candidates`} />
            <Metric
              label="ML ↔ rules agreement"
              value={s.ml_agreement_pct === null ? '—' : `${num(s.ml_agreement_pct, 1)}%`}
              tone={s.ml_agreement_pct !== null && s.ml_agreement_pct >= 50 ? 'pos' : 'neg'}
              hint={`${s.ml_calls} call(s), ${s.ml_compared} comparable`}
            />
            <Metric
              label="LLM ↔ rules agreement"
              value={s.llm_agreement_pct === null ? '—' : `${num(s.llm_agreement_pct, 1)}%`}
              tone={s.llm_agreement_pct !== null && s.llm_agreement_pct >= 50 ? 'pos' : 'neg'}
              hint={`${s.llm_calls} call(s), ${s.llm_compared} comparable`}
            />
            <Metric
              label="Blocked by limits"
              value={num(s.blocked, 0)}
              tone={s.blocked > 0 ? 'neg' : undefined}
              hint="portfolio risk said no"
            />
          </div>
        )}

        {rows.length > 0 && (
          <DataTable
            columns={[
            {
              header: 'Symbol',
              cell: (row) => (
                <button type="button" className="link" onClick={() => onSelectSymbol(row.symbol)}>
                  {row.has_open_position ? '📌 ' : ''}
                  {row.symbol}
                </button>
              ),
            },
            {
              header: 'Plan',
              cell: (row) => <span className={actionClass(row.action)}>{row.action}</span>,
            },
            {
              header: 'Rules',
              cell: (row) =>
                row.rule ? (
                  <span title={row.rule.rationale ?? undefined}>
                    <span className={actionClass(row.rule.action)}>{row.rule.action}</span>
                    <span className="muted small"> RSI {num(row.rule.rsi, 1)} · ADX {num(row.rule.adx, 1)}</span>
                  </span>
                ) : (
                  <span className="muted small">no row yet</span>
                ),
            },
            { header: 'ML model', cell: (row) => <AgentCell agent={row.ml} /> },
            { header: 'LLM agent', cell: (row) => <AgentCell agent={row.llm} /> },
            {
              header: 'Size',
              align: 'right',
              cell: (row) =>
                row.sizing.suggested_size ? (
                  <span>
                    {row.sizing.suggested_size} sh
                    <span className="muted small"> ({inr(row.sizing.suggested_value)})</span>
                  </span>
                ) : (
                  <span className="warn-text small">blocked</span>
                ),
            },
            {
              header: 'Plan levels',
              cell: (row) => (
                <span className="muted small">
                  entry {num(row.sizing.entry_price, 2)} · stop {num(row.sizing.stop_loss, 2)} · target{' '}
                  {num(row.sizing.take_profit, 2)}
                </span>
              ),
            },
            {
              header: 'Why this size',
              cell: (row) =>
                row.blocked ? (
                  <span className="warn-text small">🚫 {row.block_reason}</span>
                ) : row.sizing.correlated_with.length > 0 ? (
                  <span className="muted small">
                    reduced · correlated with {row.sizing.correlated_with.join(', ')}
                  </span>
                ) : (
                  <span className="muted small">
                    full size
                    {row.sizing.total_risk_used_pct !== null &&
                      ` · ${num(row.sizing.total_risk_used_pct, 2)}% of budget used`}
                  </span>
                ),
            },
            ]}
            rows={rows}
          />
        )}
      </Panel>
    </>
  )
}
