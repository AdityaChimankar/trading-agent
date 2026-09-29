import { useEffect, useMemo, useState } from 'react'

import { useClosePosition, useOpportunities, usePositions } from '../api/queries'
import type { Opportunity, PositionStatus } from '../api/types'
import { cx, inr, num, signedInr } from '../lib/format'
import { Panel, StateMessage } from './ui'

// The pop-out for a candidate you already hold. It reads the SAME live position
// status the Position Monitor shows (one shared React Query cache, so opening
// it costs no extra request), which means the two panels can never disagree
// about what a position is worth.
function PositionPop({ status, onClose }: { status: PositionStatus; onClose: () => void }) {
  const closePosition = useClosePosition()
  const p = status.position

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [onClose])

  return (
    <div
      className="modal-backdrop"
      role="presentation"
      onClick={onClose}
    >
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label={`Open position ${p.symbol}`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-head">
          <h3>
            📌 {p.symbol}{' '}
            <span className={p.action === 'BUY' ? 'action action-buy' : 'action action-sell'}>{p.action}</span>
          </h3>
          <button type="button" className="btn" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </div>

        <div className="metric-row">
          <div className="metric">
            <div className="metric-label">Open P&amp;L</div>
            <div className={cx('metric-value', status.pnl >= 0 ? 'metric-pos' : 'metric-neg')}>
              {signedInr(status.pnl)}
            </div>
            <div className="metric-hint">{num(status.pnl_pct, 2)}%</div>
          </div>
          <div className="metric">
            <div className="metric-label">Entry → now</div>
            <div className="metric-value">
              {num(p.entry_price, 2)} → {num(status.current_price, 2)}
            </div>
            <div className="metric-hint">
              {p.position_size} shares · {inr(status.current_value)}
            </div>
          </div>
          <div className="metric">
            <div className="metric-label">Stop / target</div>
            <div className="metric-value">
              {num(p.stop_loss, 2)} / {num(p.take_profit, 2)}
            </div>
            <div className="metric-hint">risk {inr(p.risk_amount)}</div>
          </div>
        </div>

        <p className={status.recommendation === 'HOLD' ? 'muted small' : 'warn-text small'}>
          {status.recommendation === 'HOLD' ? '🟡 HOLD' : '🔴 SELL'} — {status.reason}
        </p>

        {closePosition.isError && (
          <p className="error small">
            Close failed:{' '}
            {closePosition.error instanceof Error ? closePosition.error.message : 'unknown error'}
          </p>
        )}

        <div className="modal-actions">
          <button
            type="button"
            className="btn btn-danger"
            disabled={closePosition.isPending}
            onClick={() =>
              closePosition.mutate(p.id, {
                // Close the pop only once the book actually accepted the close -
                // closing on click would hide a failed close behind a dismissal.
                onSuccess: onClose,
              })
            }
          >
            {closePosition.isPending ? 'Closing…' : 'Close position'}
          </button>
          <button type="button" className="btn" onClick={onClose}>
            Dismiss
          </button>
        </div>
      </div>
    </div>
  )
}

function PickCard({
  opportunity,
  arrow,
  onSelect,
  openStatus,
  onPop,
}: {
  opportunity: Opportunity
  arrow: string
  onSelect: (symbol: string) => void
  openStatus: PositionStatus | null
  onPop: (status: PositionStatus) => void
}) {
  const delta = opportunity.conviction_score - opportunity.technical_score

  return (
    <article className={cx('pick', openStatus && 'pick-has-position')}>
      <header className="pick-head">
        <h3>
          {arrow} {opportunity.symbol}
        </h3>
        <span className={cx('tag', opportunity.action === 'BUY' ? 'tag-buy' : 'tag-sell')}>
          {opportunity.action}
        </span>
      </header>

      {/* The badge is the answer to "do I already hold this?" without leaving
          the panel; clicking it pops the live P&L out instead of scrolling to
          Position Monitor and hunting for the symbol. */}
      {openStatus ? (
        <button type="button" className="position-chip" onClick={() => onPop(openStatus)}>
          📌 Open {openStatus.position.action} · {openStatus.position.position_size} sh ·{' '}
          <span className={openStatus.pnl >= 0 ? 'pnl-pos' : 'pnl-neg'}>{signedInr(openStatus.pnl)}</span>
          <span className="muted small"> — click for detail</span>
        </button>
      ) : (
        <p className="muted small">No open position on this symbol yet.</p>
      )}

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
  const { data: positions } = usePositions()
  const [popped, setPopped] = useState<PositionStatus | null>(null)

  // Join by symbol rather than fetching anything new: /api/positions already
  // polls every 15s for the Position Monitor, and React Query serves this
  // component from the same cache entry.
  const statusBySymbol = useMemo(() => {
    const map = new Map<string, PositionStatus>()
    for (const status of positions ?? []) map.set(status.position.symbol, status)
    return map
  }, [positions])

  const picks = [...(data?.bullish ?? []), ...(data?.bearish ?? [])]
  const heldCount = picks.filter((o) => statusBySymbol.has(o.symbol)).length

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

      {picks.length > 0 && (
        <p className="muted small">
          {heldCount === 0
            ? 'You hold none of these candidates right now.'
            : `You already hold ${heldCount} of these ${picks.length} candidates — marked 📌 below.`}
        </p>
      )}

      <div className="two-col">
        {(
          [
            ['bullish', '🟢', data?.bullish ?? []],
            ['bearish', '🔴', data?.bearish ?? []],
          ] as const
        ).map(([direction, arrow, directionPicks]) => (
          <div key={direction} className="col">
            {directionPicks.length === 0 ? (
              <p className="muted small">No {direction} candidates right now.</p>
            ) : (
              <>
                <PickCard
                  opportunity={directionPicks[0]}
                  arrow={arrow}
                  onSelect={onSelect}
                  openStatus={statusBySymbol.get(directionPicks[0].symbol) ?? null}
                  onPop={setPopped}
                />
                {directionPicks.length > 1 && (
                  <details className="details">
                    <summary className="small">
                      Next {Math.min(4, directionPicks.length - 1)} {direction} candidates
                    </summary>
                    <ul className="plain-list small">
                      {directionPicks.slice(1, 5).map((o) => {
                        const status = statusBySymbol.get(o.symbol)
                        return (
                          <li key={o.symbol}>
                            <button type="button" className="link" onClick={() => onSelect(o.symbol)}>
                              {o.symbol}
                            </button>
                            : conviction {num(o.conviction_score, 1)} (technical {num(o.technical_score, 1)})
                            {o.blocked ? ' 🚫 blocked' : ''}
                            {status && (
                              <>
                                {' '}
                                <button type="button" className="link" onClick={() => setPopped(status)}>
                                  📌 open {signedInr(status.pnl)}
                                </button>
                              </>
                            )}
                          </li>
                        )
                      })}
                    </ul>
                  </details>
                )}
              </>
            )}
          </div>
        ))}
      </div>

      {popped && <PositionPop status={popped} onClose={() => setPopped(null)} />}
    </Panel>
  )
}
