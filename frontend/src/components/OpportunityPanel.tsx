import { useOpportunities } from '../api/queries'
import type { Opportunity } from '../api/types'
import { cx, inr, num } from '../lib/format'
import { Panel, StateMessage } from './ui'

function PickCard({
  opportunity,
  arrow,
  onSelect,
}: {
  opportunity: Opportunity
  arrow: string
  onSelect: (symbol: string) => void
}) {
  const delta = opportunity.conviction_score - opportunity.technical_score

  return (
    <article className="pick">
      <header className="pick-head">
        <h3>
          {arrow} {opportunity.symbol}
        </h3>
        <span className={cx('tag', opportunity.action === 'BUY' ? 'tag-buy' : 'tag-sell')}>
          {opportunity.action}
        </span>
      </header>

      <div className="pick-score">
        <span className="pick-score-value">{num(opportunity.conviction_score, 1)}</span>
        <span className="muted small">
          conviction{delta !== 0 ? ` (${delta > 0 ? '+' : ''}${num(delta, 1)} from signal agreement)` : ''}
        </span>
      </div>

      {opportunity.agreements.map((a) => (
        <p key={a} className="small agree">
          ✅ {a}
        </p>
      ))}
      {opportunity.disagreements.map((d) => (
        <p key={d} className="small disagree">
          ❌ {d}
        </p>
      ))}
      {opportunity.agreements.length === 0 && opportunity.disagreements.length === 0 && (
        <p className="muted small">
          Technical score only — no ML/LLM/sentiment signal available yet for this symbol.
        </p>
      )}

      {opportunity.blocked ? (
        <p className="warn-text small">🚫 Blocked: {opportunity.block_reason}</p>
      ) : opportunity.suggested_size ? (
        <>
          <p className="small">
            <strong>{opportunity.suggested_size} shares</strong> @ {num(opportunity.entry_price, 2)} (
            {inr(opportunity.suggested_value)})
          </p>
          <p className="muted small">
            Stop {num(opportunity.stop_loss, 2)} · Target {num(opportunity.take_profit, 2)}
            {opportunity.room_to_target
              ? ` · Room to next level: ₹${num(opportunity.room_to_target, 2)} (realistic R:R ${num(
                  opportunity.realistic_reward_risk,
                  2,
                )})`
              : ' · No further resistance/support level detected yet'}
          </p>
        </>
      ) : (
        <p className="muted small">Not enough data to size this trade yet.</p>
      )}

      <button type="button" className="btn" onClick={() => onSelect(opportunity.symbol)}>
        Select {opportunity.symbol}
      </button>
    </article>
  )
}

export default function OpportunityPanel({
  capital,
  onSelect,
}: {
  capital: number
  onSelect: (symbol: string) => void
}) {
  const { data, isLoading, error } = useOpportunities(capital)

  return (
    <Panel
      title="🎯 Top Opportunity"
      subtitle="Ranked by agreement across your signals — technical score, ML model, LLM agent and sentiment — plus real room to the next resistance/support level. Not a profit guarantee."
    >
      <StateMessage
        loading={isLoading}
        error={error}
        empty="No opportunities available."
        isEmpty={!data || (data.bullish.length === 0 && data.bearish.length === 0)}
      />

      <div className="two-col">
        {(
          [
            ['bullish', '🟢', data?.bullish ?? []],
            ['bearish', '🔴', data?.bearish ?? []],
          ] as const
        ).map(([direction, arrow, picks]) => (
          <div key={direction} className="col">
            {picks.length === 0 ? (
              <p className="muted small">No {direction} candidates right now.</p>
            ) : (
              <>
                <PickCard opportunity={picks[0]} arrow={arrow} onSelect={onSelect} />
                {picks.length > 1 && (
                  <details className="details">
                    <summary className="small">
                      Next {Math.min(4, picks.length - 1)} {direction} candidates
                    </summary>
                    <ul className="plain-list small">
                      {picks.slice(1, 5).map((o) => (
                        <li key={o.symbol}>
                          <button type="button" className="link" onClick={() => onSelect(o.symbol)}>
                            {o.symbol}
                          </button>
                          : conviction {num(o.conviction_score, 1)} (technical {num(o.technical_score, 1)})
                          {o.blocked ? ' 🚫 blocked' : ''}
                        </li>
                      ))}
                    </ul>
                  </details>
                )}
              </>
            )}
          </div>
        ))}
      </div>
    </Panel>
  )
}
